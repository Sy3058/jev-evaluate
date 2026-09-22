#!/usr/bin/env python3
"""Local Korean chatbot response evaluator backed by TypeSafe Jev."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import evaluation as rubric
from rendering import inspect_html
from code_runner import run_python


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "evaluations.db"
DEFAULT_ENV = ROOT / ".env"
ARTIFACTS = ROOT / "data" / "artifacts"
API_URL = "https://api.typesafe.ai/v1/systemone"
MODELS = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "claude-sonnet-5",
    "claude-opus-5",
    "gemini-3.5-flash",
]

class HTMLContentParser(HTMLParser):
    """Static content inspection, not browser rendering or HTML validation."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.blocks: list[dict[str, Any]] = []
        self.parts: list[str] = []
        self.tables = 0

    def flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        if text:
            self.blocks.append({"text": text, "in_table": "table" in self.stack,
                                "context": list(self.stack)})
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"table", "tr", "td", "th", "p", "li", "div", "br", "h1", "h2", "h3"}:
            self.flush()
        if tag == "table":
            self.tables += 1
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.stack:
            self.flush()
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def handle_data(self, data: str) -> None:
        if not set(self.stack) & {"head", "script", "style", "template"}:
            self.parts.append(data)


def html_inspection(content: str) -> dict[str, Any]:
    parser = HTMLContentParser()
    content = re.sub(r"^\s*```(?:html)?\s*\n|\n```\s*$", "", content, flags=re.I)
    parser.feed(content)
    parser.close()
    parser.flush()
    return {"method": "static_html_parser", "rendered": False,
            "limitation": "CSS visibility, layout, clipping and script-generated content are not verified.",
            "table_count": parser.tables, "blocks": parser.blocks}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_env_file(path: Path = DEFAULT_ENV) -> None:
    """Load simple KEY=VALUE pairs without overriding the process environment."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise RuntimeError(f"{path.name} {line_number}번째 줄의 형식이 올바르지 않습니다.")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not key:
            raise RuntimeError(f"{path.name} {line_number}번째 줄의 변수명이 비어 있습니다.")
        os.environ.setdefault(key, value)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args: Any) -> bool:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: Path) -> None:
    with connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                prompt TEXT NOT NULL,
                conversation_history TEXT NOT NULL DEFAULT '',
                attachment_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                model TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id, model)
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id INTEGER NOT NULL REFERENCES responses(id) ON DELETE CASCADE,
                evaluator_model TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                evidence_json TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        case_columns = {row[1] for row in db.execute("PRAGMA table_info(cases)")}
        if "evaluation_spec_json" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN evaluation_spec_json TEXT NOT NULL DEFAULT '{}'")
        columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
        if "evidence_json" not in columns:
            db.execute("ALTER TABLE evaluations ADD COLUMN evidence_json TEXT")


def call_jev(
    state: dict[str, Any], model: str, questions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY 환경변수가 설정되지 않았습니다.")
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"JEV API 오류 ({error.code}): {detail[:500]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"JEV API 연결 실패: {error.reason}") from error


class AppHandler(SimpleHTTPRequestHandler):
    db_path = DEFAULT_DB
    evaluator_model = "jev-1.13.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 5_000_000:
            raise ValueError("요청 본문이 너무 큽니다.")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON 객체가 필요합니다.")
        return value

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/config":
            self.send_json(
                {
                    "models": MODELS,
                    "criteria": rubric.LABELS,
                    "categories": rubric.CATEGORIES,
                    "rubricVersion": rubric.VERSION,
                    "apiKeyConfigured": bool(os.environ.get("TYPESAFE_API_KEY", "").strip()),
                    "evaluatorModel": self.evaluator_model,
                }
            )
            return
        if re.fullmatch(r"/api/artifacts/[a-f0-9]{64}\.png", path):
            artifact = ARTIFACTS / path.rsplit("/", 1)[-1]
            if not artifact.is_file():
                self.send_json({"error": "캡처 없음"}, 404)
                return
            body = artifact.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if re.fullmatch(r"/api/responses/\d+/history", path):
            self.send_json(self.history(int(path.split("/")[3])))
            return
        if path == "/api/results":
            self.send_json(self.list_results())
            return
        if path == "/api/export.csv":
            self.export_csv()
            return
        if path.startswith("/api/"):
            self.send_json({"error": "찾을 수 없는 API입니다."}, HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/requirements/draft":
                payload = self.read_json()
                self.send_json({"requirements": rubric.draft_requirements(str(payload.get("prompt", ""))),
                                "method": "prompt_paragraph_draft", "needsConfirmation": False})
                return
            if re.fullmatch(r"/api/cases/\d+/settings", path):
                self.send_json(self.update_settings(int(path.split("/")[3]), self.read_json()))
                return
            if path == "/api/cases":
                self.send_json(self.create_case(self.read_json()), HTTPStatus.CREATED)
                return
            if path == "/api/evaluate-pending":
                self.send_json(self.evaluate_pending())
                return
            if path.startswith("/api/responses/") and path.endswith("/evaluate"):
                response_id = int(path.split("/")[3])
                self.send_json(self.evaluate(response_id))
                return
            self.send_json({"error": "찾을 수 없는 API입니다."}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            self.send_json({"error": f"서버 오류: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def create_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        category = str(payload.get("category", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        history = str(payload.get("conversationHistory", "")).strip()
        attachment = str(payload.get("attachmentText", "")).strip()
        responses = payload.get("responses")
        if not title or not category or not prompt:
            raise ValueError("제목, 유형, 사용자 질문은 필수입니다.")
        if category not in rubric.CATEGORIES:
            raise ValueError("질문 유형은 코딩, 강의·첨부자료, 일반지식·설명, 추론·문제해결 중 하나입니다.")
        spec = rubric.normalize_spec(payload.get("evaluationSpec", {}), prompt)
        if not isinstance(responses, dict):
            raise ValueError("모델 답변 형식이 올바르지 않습니다.")
        populated = {model: str(responses.get(model, "")).strip() for model in MODELS}
        populated = {model: content for model, content in populated.items() if content}
        if not populated:
            raise ValueError("최소 한 개의 모델 답변을 입력해 주세요.")
        timestamp = now_iso()
        with connect(self.db_path) as db:
            cursor = db.execute(
                "INSERT INTO cases(title, category, prompt, conversation_history, attachment_text, created_at, evaluation_spec_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (title, category, prompt, history, attachment, timestamp, json.dumps(spec, ensure_ascii=False)),
            )
            case_id = cursor.lastrowid
            response_ids = []
            for model, content in populated.items():
                row = db.execute(
                    "INSERT INTO responses(case_id, model, content, created_at) VALUES (?, ?, ?, ?)",
                    (case_id, model, content, timestamp),
                )
                response_ids.append({"id": row.lastrowid, "model": model})
        return {"id": case_id, "responses": response_ids}

    def update_settings(self, case_id: int, payload: dict) -> dict:
        with connect(self.db_path) as db:
            case = db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not case:
                raise ValueError("케이스를 찾을 수 없습니다.")
            category = payload.get("category", case["category"])
            if category not in rubric.CATEGORIES:
                raise ValueError("네 질문 유형 중 하나를 선택하세요.")
            spec = rubric.normalize_spec(payload.get("evaluationSpec", {}), case["prompt"])
            db.execute("UPDATE cases SET category = ?, evaluation_spec_json = ? WHERE id = ?",
                       (category, json.dumps(spec, ensure_ascii=False), case_id))
        return {"id": case_id, "evaluationSpec": spec, "category": category}

    def evaluate(self, response_id: int) -> dict[str, Any]:
        # Release DB connection before browser/network work.
        with connect(self.db_path) as db:
            response_row = db.execute("SELECT * FROM responses WHERE id = ?", (response_id,)).fetchone()
            if response_row is None:
                raise ValueError("답변을 찾을 수 없습니다.")
            response = dict(response_row)
            case = dict(db.execute("SELECT * FROM cases WHERE id = ?", (response["case_id"],)).fetchone())
        spec = rubric.normalize_spec(json.loads(case["evaluation_spec_json"]), case["prompt"])
        # Validate before launching optional browser or invoking paid API.
        state, questions = rubric.prepare(case, response)
        if spec["outputFormat"] == "html":
            inspection = html_inspection(response["content"])
            inspection.update(inspect_html(response["content"], ARTIFACTS))
            state, questions = rubric.prepare(case, response, inspection)
        if spec["codeTests"]:
            execution = run_python(rubric.python_source(response["content"]), spec["codeTests"])
            state, questions = rubric.prepare(case, response, code_execution=execution)
        result = call_jev(state, self.evaluator_model, questions)
        report = rubric.parse_result(result, state, questions)
        report["settingsHash"] = rubric.digest({"category": case["category"], "spec": spec})
        report["evaluatorModel"] = str(result.get("model", self.evaluator_model))
        scores = {key: value["score"] for key, value in report["axes"].items()}
        raw = {"rubric_version": rubric.VERSION, "report": report, "quality": result,
               "state": state, "questions": questions, "spec": spec}
        with connect(self.db_path) as db:
            current = db.execute("SELECT category, evaluation_spec_json FROM cases WHERE id = ?", (case["id"],)).fetchone()
            if not current or current["category"] != case["category"] or current["evaluation_spec_json"] != case["evaluation_spec_json"]:
                raise ValueError("평가 중 공통 설정이 변경되어 저장하지 않았습니다. 다시 평가하세요.")
            cursor = db.execute(
                "INSERT INTO evaluations(response_id, evaluator_model, scores_json, evidence_json, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (response_id, report["evaluatorModel"], json.dumps(scores), None,
                 json.dumps(raw, ensure_ascii=False), now_iso()))
        return {"id": cursor.lastrowid, "responseId": response_id, "scores": scores, "report": report}

    def evaluate_pending(self) -> dict[str, Any]:
        with connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT r.id FROM responses r
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e WHERE e.response_id = r.id
                )
                ORDER BY r.id
                """
            ).fetchall()
        completed = []
        errors = []
        for row in rows:
            response_id = int(row["id"])
            try:
                completed.append(self.evaluate(response_id))
            except (RuntimeError, ValueError) as error:
                errors.append({"responseId": response_id, "error": str(error)})
                break
        return {"completed": len(completed), "remaining": len(rows) - len(completed), "errors": errors}

    def history(self, response_id: int) -> dict:
        with connect(self.db_path) as db:
            rows = db.execute("SELECT * FROM evaluations WHERE response_id = ? ORDER BY id DESC", (response_id,)).fetchall()
        items = []
        for row in rows:
            raw = json.loads(row["raw_json"])
            items.append({"id": row["id"], "createdAt": row["created_at"], "model": row["evaluator_model"],
                          "rubricVersion": raw.get("rubric_version", "generic-v1"),
                          "scores": json.loads(row["scores_json"]), "report": raw.get("report")})
        return {"items": items}

    def list_results(self) -> dict[str, Any]:
        with connect(self.db_path) as db:
            rows = db.execute("""
                SELECT c.id AS case_id, c.title, c.category, c.prompt, c.evaluation_spec_json,
                       r.id AS response_id, r.model, r.content,
                       e.id AS evaluation_id, e.evaluator_model, e.scores_json, e.raw_json,
                       e.created_at AS evaluated_at
                FROM cases c JOIN responses r ON r.case_id = c.id
                LEFT JOIN evaluations e ON e.id = (
                    SELECT e2.id FROM evaluations e2 WHERE e2.response_id = r.id ORDER BY e2.id DESC LIMIT 1
                ) ORDER BY c.id DESC, r.id ASC
                """).fetchall()
        items = []
        for row in rows:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            spec = rubric.normalize_spec(json.loads(row["evaluation_spec_json"]), row["prompt"])
            report = raw.get("report")
            settings_hash = rubric.digest({"category": row["category"], "spec": spec})
            items.append({
                "caseId": row["case_id"], "title": row["title"], "category": row["category"],
                "needsReclassification": row["category"] not in rubric.CATEGORIES,
                "prompt": row["prompt"], "responseId": row["response_id"], "model": row["model"],
                "content": row["content"], "evaluationId": row["evaluation_id"],
                "evaluatorModel": row["evaluator_model"], "evaluationSpec": spec,
                "scores": json.loads(row["scores_json"]) if row["scores_json"] else None,
                "rubricVersion": raw.get("rubric_version", "generic-v1") if row["evaluation_id"] else None,
                "report": report, "stale": bool(report and (report.get("settingsHash") != settings_hash or report.get("rubricVersion") != rubric.VERSION)),
                "evaluatedAt": row["evaluated_at"],
            })
        return {"items": items}

    def export_csv(self) -> None:
        output = io.StringIO()
        fields = ["case_id", "title", "category", "response_id", "model", "rubric_version",
                  "score_type", "evaluator_model", "evaluated_at", "stale", "needs_review", "input_hash"]
        fields += [field for key in rubric.LABELS for field in (key, key + "_status", key + "_confidence")]
        fields += ["report_json", "legacy_scores_json"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in self.list_results()["items"]:
            report = item["report"] or {}
            row = {"case_id": item["caseId"], "title": item["title"], "category": item["category"],
                   "response_id": item["responseId"], "model": item["model"],
                   "rubric_version": item["rubricVersion"], "score_type": report.get("scoreType", "legacy"),
                   "evaluator_model": item["evaluatorModel"], "evaluated_at": item["evaluatedAt"],
                   "stale": item["stale"], "needs_review": report.get("needsReview"), "input_hash": report.get("inputHash"),
                   "report_json": json.dumps(report, ensure_ascii=False),
                   "legacy_scores_json": json.dumps(item["scores"], ensure_ascii=False) if not report else ""}
            for key, axis in report.get("axes", {}).items():
                row[key], row[key + "_status"], row[key + "_confidence"] = axis["score"], axis["status"], axis["confidence"]
            # Keep spreadsheet formulas from being executed when importing user-supplied cells.
            row = {key: "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
                   for key, value in row.items()}
            writer.writerow(row)
        body = output.getvalue().encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="jev-evaluations.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="JEV 챗봇 평가 로컬 웹 앱")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--model", default="jev-1.13.0")
    args = parser.parse_args()
    load_env_file()
    init_db(args.db)
    AppHandler.db_path = args.db
    AppHandler.evaluator_model = args.model
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"JEV 평가기: http://{args.host}:{args.port}")
    print(f"데이터베이스: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
