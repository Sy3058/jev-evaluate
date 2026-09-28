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
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import evaluation as rubric
import bot_evaluation
from rendering import inspect_html
from code_runner import run_python
from generated_files import MAX_FILE_BYTES, inspect_generated_file


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
    "gemini-3.8-flash",
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


def settings_digest(case: dict, spec: dict) -> str:
    value = {"category": case["category"], "spec": spec}
    if case["evaluation_mode"] == "ai_bot":
        value.update({"mode": "ai_bot", "botId": case["bot_id"], "botVersion": case["bot_version"],
                      "testType": case["test_type"], "expectedBehavior": case["expected_behavior"],
                      "checkFocus": case["check_focus"], "inputExample": case["input_example"]})
    return rubric.digest(value)


def evaluation_fingerprint(case: dict, response: dict, artifact: dict | None,
                           spec: dict, evaluator_model: str) -> str:
    return rubric.digest({"settings": settings_digest(case, spec), "response": response["content"],
                          "responseModel": response["model"], "artifactSha256": artifact["sha256"] if artifact else None,
                          "artifactFilename": artifact["filename"] if artifact else None,
                          "evaluatorModel": evaluator_model, "rubricVersion": rubric.VERSION,
                          "botQualityVersion": bot_evaluation.QUALITY_VERSION if case["evaluation_mode"] == "ai_bot" else None,
                          "botInstructionVersion": bot_evaluation.VERSION if case["evaluation_mode"] == "ai_bot" else None})


class EvaluationInProgress(ValueError):
    pass


def bot_test_fields(test_type: Any, expected_behavior: Any, check_focus: Any = "",
                    input_example: Any = "") -> tuple[str, str, str, str]:
    if (not isinstance(test_type, str) or test_type not in {"normal", "exception"} or
            not isinstance(expected_behavior, str) or not isinstance(check_focus, str) or
            not isinstance(input_example, str)):
        raise ValueError("AI Bot 테스트 유형과 기대 행동 형식이 올바르지 않습니다.")
    expected_behavior = expected_behavior.strip()
    check_focus, input_example = check_focus.strip(), input_example.strip()
    if max(len(expected_behavior), len(check_focus), len(input_example)) > 4000:
        raise ValueError("점검 관점, 기대 결과, 입력 예시는 각각 4000자 이하로 입력하세요.")
    if test_type == "exception" and not expected_behavior:
        raise ValueError("예외 테스트에는 기대 행동을 입력하세요.")
    return test_type, expected_behavior, check_focus, input_example


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
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_versions (
                bot_id INTEGER NOT NULL REFERENCES bots(id),
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                instructions TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(bot_id, version)
            );
            CREATE TABLE IF NOT EXISTS response_artifacts (
                response_id INTEGER PRIMARY KEY REFERENCES responses(id) ON DELETE CASCADE,
                filename TEXT NOT NULL,
                format TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                storage_name TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                text_truncated INTEGER NOT NULL DEFAULT 0,
                text_available INTEGER NOT NULL DEFAULT 1,
                extraction_note TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_jobs (
                response_id INTEGER PRIMARY KEY REFERENCES responses(id) ON DELETE CASCADE,
                token TEXT NOT NULL,
                started_at TEXT NOT NULL
            );
            """
        )
        case_columns = {row[1] for row in db.execute("PRAGMA table_info(cases)")}
        if "evaluation_spec_json" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN evaluation_spec_json TEXT NOT NULL DEFAULT '{}'")
        if "evaluation_mode" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN evaluation_mode TEXT NOT NULL DEFAULT 'model'")
        if "bot_id" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN bot_id INTEGER")
        if "bot_version" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN bot_version INTEGER")
        if "test_type" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN test_type TEXT NOT NULL DEFAULT 'normal'")
        if "expected_behavior" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN expected_behavior TEXT NOT NULL DEFAULT ''")
        if "check_focus" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN check_focus TEXT NOT NULL DEFAULT ''")
        if "input_example" not in case_columns:
            db.execute("ALTER TABLE cases ADD COLUMN input_example TEXT NOT NULL DEFAULT ''")
        version_columns = {row[1] for row in db.execute("PRAGMA table_info(bot_versions)")}
        if "name" not in version_columns:
            db.execute("ALTER TABLE bot_versions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE bot_versions SET name = (SELECT name FROM bots WHERE bots.id = bot_versions.bot_id)")
        if "description" not in version_columns:
            db.execute("ALTER TABLE bot_versions ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE bot_versions SET description = (SELECT description FROM bots WHERE bots.id = bot_versions.bot_id)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
        if "evidence_json" not in columns:
            db.execute("ALTER TABLE evaluations ADD COLUMN evidence_json TEXT")
        artifact_columns = {row[1] for row in db.execute("PRAGMA table_info(response_artifacts)")}
        if "text_available" not in artifact_columns:
            db.execute("ALTER TABLE response_artifacts ADD COLUMN text_available INTEGER NOT NULL DEFAULT 1")


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

    def read_upload(self) -> tuple[str, bytes]:
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdecimal():
            raise ValueError("파일 크기를 확인할 수 없습니다.")
        length = int(raw_length)
        if length < 1 or length > MAX_FILE_BYTES:
            raise ValueError("생성 파일은 1바이트 이상 10MB 이하로 올려 주세요.")
        filename = unquote(self.headers.get("X-File-Name", ""))
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("파일 전송이 완료되지 않았습니다.")
        return filename, data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        requested_mode = query.get("mode", ["model"])[0]
        if path in {"/api/results", "/api/export.csv"} and requested_mode not in {"model", "ai_bot"}:
            self.send_json({"error": "평가 유형이 올바르지 않습니다."}, HTTPStatus.BAD_REQUEST)
            return
        requested_bot = query.get("botId", [""])[0]
        if path in {"/api/results", "/api/export.csv"} and requested_bot and (requested_mode != "ai_bot" or not requested_bot.isdecimal() or int(requested_bot) < 1):
            self.send_json({"error": "Bot 필터가 올바르지 않습니다."}, HTTPStatus.BAD_REQUEST)
            return
        bot_id = int(requested_bot) if requested_bot else None
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
        if re.fullmatch(r"/api/responses/\d+/artifact", path):
            self.download_artifact(int(path.split("/")[3]))
            return
        if path == "/api/bots":
            self.send_json(self.list_bots())
            return
        if path == "/api/results":
            self.send_json(self.list_results(requested_mode, bot_id))
            return
        if path == "/api/export.csv":
            self.export_csv(requested_mode, bot_id)
            return
        if path.startswith("/api/"):
            self.send_json({"error": "찾을 수 없는 API입니다."}, HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/bots":
                self.send_json(self.create_bot(self.read_json()), HTTPStatus.CREATED)
                return
            if re.fullmatch(r"/api/bots/\d+", path):
                self.send_json(self.update_bot(int(path.split("/")[3]), self.read_json()))
                return
            if re.fullmatch(r"/api/cases/\d+/bot-version", path):
                self.send_json(self.update_case_bot_version(int(path.split("/")[3]), self.read_json()))
                return
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
            if re.fullmatch(r"/api/responses/\d+/artifact", path):
                filename, data = self.read_upload()
                self.send_json(self.save_artifact(int(path.split("/")[3]), filename, data))
                return
            if path == "/api/evaluate-pending":
                payload = self.read_json()
                self.send_json(self.evaluate_pending(payload.get("mode", "model"), payload.get("botId")))
                return
            if path.startswith("/api/responses/") and path.endswith("/evaluate"):
                response_id = int(path.split("/")[3])
                payload = self.read_json()
                self.send_json(self.evaluate(response_id, force=payload.get("force", False)))
                return
            self.send_json({"error": "찾을 수 없는 API입니다."}, HTTPStatus.NOT_FOUND)
        except EvaluationInProgress as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            self.send_json({"error": f"서버 오류: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def list_bots(self) -> dict[str, Any]:
        with connect(self.db_path) as db:
            rows = db.execute("""SELECT b.*, v.instructions,
                (SELECT COUNT(*) FROM cases c WHERE c.bot_id = b.id) AS case_count
                FROM bots b JOIN bot_versions v ON v.bot_id = b.id AND v.version = b.current_version
                ORDER BY b.archived, b.id DESC""").fetchall()
        return {"items": [{"id": row["id"], "name": row["name"], "description": row["description"],
                           "instructions": row["instructions"], "version": row["current_version"],
                           "archived": bool(row["archived"]), "caseCount": row["case_count"],
                           "updatedAt": row["updated_at"]} for row in rows]}

    def bot_fields(self, payload: dict) -> tuple[str, str, str]:
        if any(not isinstance(payload.get(key), str) for key in ("name", "description", "instructions")):
            raise ValueError("Bot 이름, 설명, 지침은 문자열이어야 합니다.")
        name = payload["name"].strip()
        description = payload["description"].strip()
        instructions = payload["instructions"].strip()
        if not name or not description or not instructions:
            raise ValueError("Bot 이름, 설명, 지침은 모두 필요합니다.")
        if len(name) > 120 or len(description) > 2000 or len(instructions) > 30000:
            raise ValueError("Bot 이름은 120자, 설명은 2000자, 지침은 30000자 이하로 입력하세요.")
        bot_evaluation.instruction_parts(instructions)
        return name, description, instructions

    def create_bot(self, payload: dict) -> dict:
        name, description, instructions = self.bot_fields(payload)
        timestamp = now_iso()
        with connect(self.db_path) as db:
            cursor = db.execute("INSERT INTO bots(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                (name, description, timestamp, timestamp))
            bot_id = cursor.lastrowid
            db.execute("INSERT INTO bot_versions(bot_id, version, name, description, instructions, created_at) VALUES (?, 1, ?, ?, ?, ?)",
                       (bot_id, name, description, instructions, timestamp))
        return {"id": bot_id, "version": 1}

    def update_bot(self, bot_id: int, payload: dict) -> dict:
        with connect(self.db_path) as db:
            current = db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone()
            if not current:
                raise ValueError("Bot을 찾을 수 없습니다.")
            if "archived" in payload:
                archived = payload["archived"]
                if type(archived) is not bool:
                    raise ValueError("보관 상태는 참/거짓이어야 합니다.")
                db.execute("UPDATE bots SET archived = ?, updated_at = ? WHERE id = ?",
                           (int(archived), now_iso(), bot_id))
                return {"id": bot_id, "archived": archived, "version": current["current_version"]}
            name, description, instructions = self.bot_fields(payload)
            version = current["current_version"] + 1
            timestamp = now_iso()
            db.execute("UPDATE bots SET name = ?, description = ?, current_version = ?, updated_at = ? WHERE id = ?",
                       (name, description, version, timestamp, bot_id))
            db.execute("INSERT INTO bot_versions(bot_id, version, name, description, instructions, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (bot_id, version, name, description, instructions, timestamp))
        return {"id": bot_id, "version": version}

    def update_case_bot_version(self, case_id: int, payload: dict) -> dict:
        with connect(self.db_path) as db:
            case = db.execute("SELECT evaluation_mode, bot_id FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not case or case["evaluation_mode"] != "ai_bot":
                raise ValueError("AI Bot 케이스를 찾을 수 없습니다.")
            bot = db.execute("SELECT current_version FROM bots WHERE id = ? AND archived = 0", (case["bot_id"],)).fetchone()
            if not bot:
                raise ValueError("Bot을 찾을 수 없습니다.")
            version = payload.get("version", bot["current_version"])
            if type(version) is not int or version != bot["current_version"]:
                raise ValueError("현재 Bot 지침 버전만 적용할 수 있습니다.")
            db.execute("UPDATE cases SET bot_version = ? WHERE id = ?", (version, case_id))
        return {"id": case_id, "botVersion": version}

    def save_artifact(self, response_id: int, filename: str, data: bytes) -> dict:
        with connect(self.db_path) as db:
            if not db.execute("SELECT 1 FROM responses WHERE id = ?", (response_id,)).fetchone():
                raise ValueError("답변을 찾을 수 없습니다.")
        details = inspect_generated_file(filename, data)
        storage_name = details["sha256"] + details["extension"]
        uploads_dir = self.db_path.parent / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        target = uploads_dir / storage_name
        if not target.exists():
            target.write_bytes(data)
        with connect(self.db_path) as db:
            db.execute("""INSERT INTO response_artifacts
                (response_id, filename, format, size, sha256, storage_name, extracted_text,
                 text_truncated, text_available, extraction_note, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(response_id) DO UPDATE SET
                  filename=excluded.filename, format=excluded.format, size=excluded.size,
                  sha256=excluded.sha256, storage_name=excluded.storage_name,
                  extracted_text=excluded.extracted_text, text_truncated=excluded.text_truncated,
                  text_available=excluded.text_available,
                  extraction_note=excluded.extraction_note, uploaded_at=excluded.uploaded_at""",
                (response_id, details["filename"], details["format"], details["size"],
                 details["sha256"], storage_name, details["extractedText"],
                 int(details["textTruncated"]), int(details["textAvailable"]), details["extractionNote"], now_iso()))
        return {"responseId": response_id, "filename": details["filename"], "format": details["format"],
                "size": details["size"], "sha256": details["sha256"],
                "textTruncated": details["textTruncated"], "textAvailable": details["textAvailable"],
                "extractionNote": details["extractionNote"]}

    def download_artifact(self, response_id: int) -> None:
        with connect(self.db_path) as db:
            row = db.execute("SELECT filename, storage_name FROM response_artifacts WHERE response_id = ?",
                             (response_id,)).fetchone()
        if not row or not re.fullmatch(r"[a-f0-9]{64}\.(?:html|htm|pdf|xlsx)", row["storage_name"]):
            self.send_json({"error": "생성 파일을 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
            return
        target = self.db_path.parent / "uploads" / row["storage_name"]
        if not target.is_file():
            self.send_json({"error": "저장된 파일을 찾을 수 없습니다."}, HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(row['filename'])}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def create_case(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        category = str(payload.get("category", "")).strip()
        prompt = str(payload.get("prompt", "")).strip()
        history = str(payload.get("conversationHistory", "")).strip()
        attachment = str(payload.get("attachmentText", "")).strip()
        responses = payload.get("responses")
        mode = payload.get("evaluationMode", "model")
        if mode not in {"model", "ai_bot"}:
            raise ValueError("평가 유형이 올바르지 않습니다.")
        test_type, expected_behavior, check_focus, input_example = (
            bot_test_fields(payload.get("testType", "normal"), payload.get("expectedBehavior", ""),
                            payload.get("checkFocus", ""), payload.get("inputExample", ""))
            if mode == "ai_bot" else ("normal", "", "", ""))
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
            bot_id = bot_version = None
            if mode == "ai_bot":
                bot_id = payload.get("botId")
                if type(bot_id) is not int:
                    raise ValueError("AI Bot을 선택해 주세요.")
                bot = db.execute("SELECT current_version FROM bots WHERE id = ? AND archived = 0", (bot_id,)).fetchone()
                if not bot:
                    raise ValueError("사용할 수 있는 AI Bot을 선택해 주세요.")
                bot_version = bot["current_version"]
            cursor = db.execute(
                "INSERT INTO cases(title, category, prompt, conversation_history, attachment_text, created_at, evaluation_spec_json, evaluation_mode, bot_id, bot_version, test_type, expected_behavior, check_focus, input_example) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, category, prompt, history, attachment, timestamp, json.dumps(spec, ensure_ascii=False), mode, bot_id, bot_version, test_type, expected_behavior, check_focus, input_example),
            )
            case_id = cursor.lastrowid
            response_ids = []
            for model, content in populated.items():
                row = db.execute(
                    "INSERT INTO responses(case_id, model, content, created_at) VALUES (?, ?, ?, ?)",
                    (case_id, model, content, timestamp),
                )
                response_ids.append({"id": row.lastrowid, "model": model})
        return {"id": case_id, "responses": response_ids, "evaluationMode": mode, "botId": bot_id,
                "botVersion": bot_version, "testType": test_type, "expectedBehavior": expected_behavior,
                "checkFocus": check_focus, "inputExample": input_example}

    def update_settings(self, case_id: int, payload: dict) -> dict:
        with connect(self.db_path) as db:
            case = db.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not case:
                raise ValueError("케이스를 찾을 수 없습니다.")
            category = payload.get("category", case["category"])
            if category not in rubric.CATEGORIES:
                raise ValueError("네 질문 유형 중 하나를 선택하세요.")
            spec = rubric.normalize_spec(payload.get("evaluationSpec", {}), case["prompt"])
            test_type, expected_behavior, check_focus, input_example = (
                bot_test_fields(payload.get("testType", case["test_type"]),
                                payload.get("expectedBehavior", case["expected_behavior"]),
                                payload.get("checkFocus", case["check_focus"]),
                                payload.get("inputExample", case["input_example"]))
                if case["evaluation_mode"] == "ai_bot" else ("normal", "", "", ""))
            db.execute("UPDATE cases SET category = ?, evaluation_spec_json = ?, test_type = ?, expected_behavior = ?, check_focus = ?, input_example = ? WHERE id = ?",
                       (category, json.dumps(spec, ensure_ascii=False), test_type, expected_behavior, check_focus, input_example, case_id))
        return {"id": case_id, "evaluationSpec": spec, "category": category,
                "testType": test_type, "expectedBehavior": expected_behavior,
                "checkFocus": check_focus, "inputExample": input_example}

    def cached_evaluation(self, response_id: int) -> dict[str, Any] | None:
        with connect(self.db_path) as db:
            response_row = db.execute("SELECT * FROM responses WHERE id = ?", (response_id,)).fetchone()
            if response_row is None:
                raise ValueError("답변을 찾을 수 없습니다.")
            response = dict(response_row)
            case = dict(db.execute("SELECT * FROM cases WHERE id = ?", (response["case_id"],)).fetchone())
            artifact_row = db.execute("SELECT * FROM response_artifacts WHERE response_id = ?", (response_id,)).fetchone()
            artifact = dict(artifact_row) if artifact_row else None
            latest = db.execute("SELECT * FROM evaluations WHERE response_id = ? ORDER BY id DESC LIMIT 1",
                                (response_id,)).fetchone()
        if latest is None:
            return None
        raw = json.loads(latest["raw_json"])
        report = raw.get("report")
        if not report:
            return None
        spec = rubric.normalize_spec(json.loads(case["evaluation_spec_json"]), case["prompt"])
        fingerprint = evaluation_fingerprint(case, response, artifact, spec, self.evaluator_model)
        expected_version = bot_evaluation.QUALITY_VERSION if case["evaluation_mode"] == "ai_bot" else rubric.VERSION
        if (report.get("settingsHash") != settings_digest(case, spec) or
                report.get("rubricVersion") != expected_version or
                case["evaluation_mode"] == "ai_bot" and report.get("botAssessment", {}).get("version") != bot_evaluation.VERSION or
                report.get("artifactSha256") != (artifact["sha256"] if artifact else None) or
                (report.get("generatedArtifact") or {}).get("filename") != (artifact["filename"] if artifact else None) or
                report.get("requestedEvaluatorModel", report.get("evaluatorModel")) != self.evaluator_model or
                report.get("evaluationFingerprint", fingerprint) != fingerprint):
            return None
        return {"id": latest["id"], "responseId": response_id, "scores": json.loads(latest["scores_json"]),
                "report": report, "cached": True}

    def evaluate(self, response_id: int, force: bool = False) -> dict[str, Any]:
        if type(force) is not bool:
            raise ValueError("강제 재평가 옵션이 올바르지 않습니다.")
        if not force:
            cached = self.cached_evaluation(response_id)
            if cached:
                return cached
        token = uuid.uuid4().hex
        cutoff = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        with connect(self.db_path) as db:
            db.execute("DELETE FROM evaluation_jobs WHERE started_at < ?", (cutoff,))
            try:
                db.execute("INSERT INTO evaluation_jobs(response_id, token, started_at) VALUES (?, ?, ?)",
                           (response_id, token, now_iso()))
            except sqlite3.IntegrityError as error:
                raise EvaluationInProgress("이 답변은 이미 평가 중입니다. 완료 후 결과를 새로고침해 주세요.") from error
        try:
            if not force:
                cached = self.cached_evaluation(response_id)
                if cached:
                    return cached
            return self._run_evaluation(response_id, token)
        finally:
            with connect(self.db_path) as db:
                db.execute("DELETE FROM evaluation_jobs WHERE response_id = ? AND token = ?", (response_id, token))

    def _run_evaluation(self, response_id: int, token: str) -> dict[str, Any]:
        started = time.monotonic()
        html_ms = 0
        # Release DB connection before browser/network work.
        with connect(self.db_path) as db:
            response_row = db.execute("SELECT * FROM responses WHERE id = ?", (response_id,)).fetchone()
            if response_row is None:
                raise ValueError("답변을 찾을 수 없습니다.")
            response = dict(response_row)
            case = dict(db.execute("SELECT * FROM cases WHERE id = ?", (response["case_id"],)).fetchone())
            artifact_row = db.execute("SELECT * FROM response_artifacts WHERE response_id = ?", (response_id,)).fetchone()
            artifact = dict(artifact_row) if artifact_row else None
            bot_row = None
            if case["evaluation_mode"] == "ai_bot":
                bot_row = db.execute("SELECT * FROM bot_versions WHERE bot_id = ? AND version = ?",
                                     (case["bot_id"], case["bot_version"])).fetchone()
                if not bot_row:
                    raise ValueError("적용할 Bot 지침 버전을 찾을 수 없습니다.")
        spec = rubric.normalize_spec(json.loads(case["evaluation_spec_json"]), case["prompt"])
        effective_spec = ({**spec, "outputFormat": "html"}
                          if artifact and artifact["format"] == "html" and spec["outputFormat"] == "text" else spec)
        evaluation_case = ({**case, "evaluation_spec_json": json.dumps(effective_spec, ensure_ascii=False)}
                           if effective_spec is not spec else case)
        artifact_text = artifact["extracted_text"] if artifact else ""
        repeated_artifact = bool(artifact_text and artifact_text.strip() == response["content"].strip())
        effective_content = response["content"] + (
            "" if not artifact_text or repeated_artifact else
            f"\n\n[생성 파일 {artifact['filename']}에서 추출한 내용]\n{artifact_text}")
        effective_response = {**response, "content": effective_content}
        artifact_summary = ({"filename": artifact["filename"], "format": artifact["format"],
                             "size": artifact["size"], "sha256": artifact["sha256"],
                             "textTruncated": bool(artifact["text_truncated"]),
                             "textAvailable": bool(artifact["text_available"]),
                             "extractionNote": artifact["extraction_note"]} if artifact else None)
        if bot_row and (len(effective_content) + len(case["prompt"]) +
                        len(case["conversation_history"]) + len(case["attachment_text"]) +
                        len(json.dumps(spec, ensure_ascii=False)) + len(bot_row["instructions"]) +
                        len(case["check_focus"]) + len(case["expected_behavior"]) + len(case["input_example"]) > 160000):
            raise ValueError("Bot 지침을 포함한 평가 입력이 160,000자를 초과합니다.")
        # Validate before launching optional browser or invoking paid API.
        state, questions = rubric.prepare(evaluation_case, effective_response)
        if effective_spec["outputFormat"] == "html":
            html_started = time.monotonic()
            html_content = (artifact_text if artifact and artifact["format"] == "html" else response["content"])
            if artifact and artifact["format"] == "html" and artifact["text_truncated"]:
                html_content = (self.db_path.parent / "uploads" / artifact["storage_name"]).read_text(encoding="utf-8-sig")
            inspection = html_inspection(html_content)
            inspection.update(inspect_html(html_content, self.db_path.parent / "artifacts"))
            state, questions = rubric.prepare(evaluation_case, effective_response, inspection)
            html_ms = round((time.monotonic() - html_started) * 1000)
        if spec["codeTests"]:
            execution = run_python(rubric.python_source(effective_content), spec["codeTests"])
            state, questions = rubric.prepare(evaluation_case, effective_response, code_execution=execution)
        if artifact_summary:
            state["generated_artifact"] = artifact_summary
        if bot_row:
            bot_evaluation.add_questions(state, questions, bot_row["instructions"],
                                         case["test_type"], case["expected_behavior"], case["check_focus"],
                                         case["title"], case["input_example"], artifact_summary)
            state["bot_version"] = case["bot_version"]
            state["bot_id"] = case["bot_id"]
        full_inspection = state.get("inspection")
        if bot_row and full_inspection:
            # The static blocks and browser text repeat the same HTML body. Quote checks
            # were already computed above; retain the full observation in the report.
            state["inspection"] = {key: value for key, value in full_inspection.items() if key != "blocks"}
            if (full_inspection.get("observations") or {}).get("visibleText"):
                state["visible_report_text"] = None
        jev_started = time.monotonic()
        result = call_jev(state, self.evaluator_model, questions)
        jev_ms = round((time.monotonic() - jev_started) * 1000)
        report = rubric.parse_result(result, state, questions)
        if full_inspection is not None:
            report["inspection"] = full_inspection
        if bot_row:
            report["botAssessment"] = bot_evaluation.parse_result(result, state)
            report["groundingIssue"] = result["answers"]["bot_grounding_issue"]["choice"]
            report["quoteObservations"] = state["quote_observations"]
            report["responseLengthIssue"] = result["answers"]["bot_length_issue"]["choice"]
            report["botVersion"] = case["bot_version"]
            report["botName"] = bot_row["name"]
            report["botInstructions"] = bot_row["instructions"]
            report["testType"] = case["test_type"]
            report["expectedBehavior"] = case["expected_behavior"]
            report["checkFocus"] = case["check_focus"]
            report["inputExample"] = case["input_example"]
            bot_evaluation.apply_discrete_quality(result, state, report)
            report["rubricVersion"] = bot_evaluation.QUALITY_VERSION
        report["settingsHash"] = settings_digest(case, spec)
        report["artifactSha256"] = artifact["sha256"] if artifact else None
        report["generatedArtifact"] = artifact_summary
        report["effectiveOutputFormat"] = effective_spec["outputFormat"]
        report["evaluationFingerprint"] = evaluation_fingerprint(case, response, artifact, spec, self.evaluator_model)
        if artifact and (artifact["text_truncated"] or not artifact["text_available"]):
            report["issues"].append("생성 파일 내용 추출이 불완전합니다. 사람 검토가 필요합니다.")
            report["needsReview"] = True
        report["evaluatorModel"] = str(result.get("model", self.evaluator_model))
        report["requestedEvaluatorModel"] = self.evaluator_model
        report["timings"] = {"htmlInspectionMs": html_ms, "jevMs": jev_ms,
                             "totalMs": round((time.monotonic() - started) * 1000)}
        scores = {key: value["score"] for key, value in report["axes"].items()}
        raw = {"rubric_version": report["rubricVersion"], "report": report, "quality": result,
               "state": state, "questions": questions, "spec": effective_spec}
        with connect(self.db_path) as db:
            job = db.execute("SELECT token FROM evaluation_jobs WHERE response_id = ?", (response_id,)).fetchone()
            if not job or job["token"] != token:
                raise EvaluationInProgress("평가 작업이 교체되어 이전 결과를 저장하지 않았습니다.")
            current = db.execute("SELECT category, evaluation_spec_json, bot_version, test_type, expected_behavior, check_focus, input_example FROM cases WHERE id = ?", (case["id"],)).fetchone()
            if not current or any(current[key] != case[key] for key in current.keys()):
                raise ValueError("평가 중 공통 설정이 변경되어 저장하지 않았습니다. 다시 평가하세요.")
            current_artifact = db.execute("SELECT sha256, filename FROM response_artifacts WHERE response_id = ?", (response_id,)).fetchone()
            if ((current_artifact["sha256"] if current_artifact else None) != report["artifactSha256"] or
                    (current_artifact["filename"] if current_artifact else None) !=
                    (report["generatedArtifact"] or {}).get("filename")):
                raise ValueError("평가 중 생성 파일이 변경되어 저장하지 않았습니다. 다시 평가하세요.")
            cursor = db.execute(
                "INSERT INTO evaluations(response_id, evaluator_model, scores_json, evidence_json, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (response_id, report["evaluatorModel"], json.dumps(scores), None,
                 json.dumps(raw, ensure_ascii=False), now_iso()))
        return {"id": cursor.lastrowid, "responseId": response_id, "scores": scores, "report": report}

    def evaluate_pending(self, mode: str = "model", bot_id: int | None = None) -> dict[str, Any]:
        if mode not in {"model", "ai_bot"}:
            raise ValueError("평가 유형이 올바르지 않습니다.")
        if bot_id is not None and (mode != "ai_bot" or type(bot_id) is not int or bot_id < 1):
            raise ValueError("Bot 필터가 올바르지 않습니다.")
        with connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT r.id FROM responses r JOIN cases c ON c.id = r.case_id
                WHERE NOT EXISTS (
                    SELECT 1 FROM evaluations e WHERE e.response_id = r.id
                ) AND c.evaluation_mode = ? AND (? IS NULL OR c.bot_id = ?)
                ORDER BY r.id
                """, (mode, bot_id, bot_id)
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

    def list_results(self, mode: str = "model", bot_id: int | None = None) -> dict[str, Any]:
        if mode not in {"model", "ai_bot"}:
            raise ValueError("평가 유형이 올바르지 않습니다.")
        if bot_id is not None and (mode != "ai_bot" or type(bot_id) is not int or bot_id < 1):
            raise ValueError("Bot 필터가 올바르지 않습니다.")
        with connect(self.db_path) as db:
            rows = db.execute("""
                SELECT c.id AS case_id, c.title, c.category, c.prompt, c.evaluation_spec_json,
                       c.evaluation_mode, c.bot_id, c.bot_version, c.test_type, c.expected_behavior,
                       c.check_focus, c.input_example, bv.name AS bot_name,
                       b.current_version AS bot_current_version, bv.instructions AS bot_instructions,
                       r.id AS response_id, r.model, r.content,
                       a.filename AS artifact_filename, a.format AS artifact_format,
                       a.size AS artifact_size, a.sha256 AS artifact_sha256,
                       a.text_truncated AS artifact_text_truncated, a.text_available AS artifact_text_available,
                       a.extraction_note AS artifact_extraction_note,
                       a.storage_name AS artifact_storage_name,
                       e.id AS evaluation_id, e.evaluator_model, e.scores_json, e.raw_json,
                       e.created_at AS evaluated_at
                FROM cases c JOIN responses r ON r.case_id = c.id
                LEFT JOIN bots b ON b.id = c.bot_id
                LEFT JOIN bot_versions bv ON bv.bot_id = c.bot_id AND bv.version = c.bot_version
                LEFT JOIN response_artifacts a ON a.response_id = r.id
                LEFT JOIN evaluations e ON e.id = (
                    SELECT e2.id FROM evaluations e2 WHERE e2.response_id = r.id ORDER BY e2.id DESC LIMIT 1
                ) WHERE c.evaluation_mode = ? AND (? IS NULL OR c.bot_id = ?) ORDER BY c.id DESC, r.id ASC
                """, (mode, bot_id, bot_id)).fetchall()
        items = []
        for row in rows:
            raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
            spec = rubric.normalize_spec(json.loads(row["evaluation_spec_json"]), row["prompt"])
            report = raw.get("report")
            settings_hash = settings_digest(row, spec)
            stale_reasons = []
            if report:
                if report.get("settingsHash") != settings_hash:
                    stale_reasons.append("공통 설정 또는 Bot 지침 버전 변경" if mode == "ai_bot" else "공통 설정 변경")
                if report.get("rubricVersion") != (bot_evaluation.QUALITY_VERSION if mode == "ai_bot" else rubric.VERSION):
                    stale_reasons.append("평가 기준 변경")
                if mode == "ai_bot" and report.get("botAssessment", {}).get("version") != bot_evaluation.VERSION:
                    stale_reasons.append("Bot 지침 판정 기준 변경")
                if (report.get("artifactSha256") != row["artifact_sha256"] or
                        (report.get("generatedArtifact") or {}).get("filename") != row["artifact_filename"]):
                    stale_reasons.append("생성 파일 변경")
                if report.get("requestedEvaluatorModel", report.get("evaluatorModel")) != self.evaluator_model:
                    stale_reasons.append("평가 모델 변경")
                if report.get("evaluationFingerprint") and report["evaluationFingerprint"] != evaluation_fingerprint(
                        row, {"content": row["content"], "model": row["model"]},
                        {"sha256": row["artifact_sha256"], "filename": row["artifact_filename"]}
                        if row["artifact_sha256"] else None, spec, self.evaluator_model):
                    stale_reasons.append("평가 입력 변경")
            artifact_summary = ({"filename": row["artifact_filename"], "format": row["artifact_format"],
                                 "size": row["artifact_size"], "sha256": row["artifact_sha256"],
                                 "textTruncated": bool(row["artifact_text_truncated"]),
                                 "textAvailable": bool(row["artifact_text_available"]),
                                 "extractionNote": row["artifact_extraction_note"],
                                 "downloadUrl": f"/api/responses/{row['response_id']}/artifact"}
                                if row["artifact_sha256"] else None)
            items.append({
                "caseId": row["case_id"], "title": row["title"], "category": row["category"],
                "evaluationMode": row["evaluation_mode"], "botId": row["bot_id"],
                "botVersion": row["bot_version"], "botName": row["bot_name"],
                "testType": row["test_type"], "expectedBehavior": row["expected_behavior"],
                "checkFocus": row["check_focus"], "inputExample": row["input_example"],
                "botCurrentVersion": row["bot_current_version"], "botInstructions": row["bot_instructions"],
                "needsReclassification": row["category"] not in rubric.CATEGORIES,
                "prompt": row["prompt"], "responseId": row["response_id"], "model": row["model"],
                "content": row["content"], "evaluationId": row["evaluation_id"],
                "artifact": artifact_summary,
                "evaluatorModel": row["evaluator_model"], "evaluationSpec": spec,
                "scores": json.loads(row["scores_json"]) if row["scores_json"] else None,
                "rubricVersion": raw.get("rubric_version", "generic-v1") if row["evaluation_id"] else None,
                "report": report, "stale": bool(stale_reasons), "staleReasons": stale_reasons,
                "evaluatedAt": row["evaluated_at"],
            })
        return {"items": items}

    def export_csv(self, mode: str = "model", bot_id: int | None = None) -> None:
        if mode not in {"model", "ai_bot"}:
            self.send_json({"error": "평가 유형이 올바르지 않습니다."}, HTTPStatus.BAD_REQUEST)
            return
        output = io.StringIO()
        fields = ["case_id", "title", "category", "response_id", "model", "rubric_version",
                  "evaluation_mode", "bot_id", "bot_name", "bot_version", "scenario_type", "test_scenario",
                  "input_example", "check_focus", "expected_result", "bot_status", "bot_rules_json",
                  "artifact_filename", "artifact_format", "artifact_size", "artifact_sha256", "artifact_text_truncated",
                  "score_type", "evaluator_model", "evaluated_at", "stale", "needs_review", "input_hash"]
        fields += [field for key in rubric.LABELS for field in (key, key + "_status", key + "_confidence")]
        fields += ["quality_violations_json", "quality_pending_json", "report_json", "legacy_scores_json"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in self.list_results(mode, bot_id)["items"]:
            report = item["report"] or {}
            row = {"case_id": item["caseId"], "title": item["title"], "category": item["category"],
                   "evaluation_mode": item["evaluationMode"], "bot_id": item["botId"],
                   "bot_name": item["botName"], "bot_version": item["botVersion"],
                   "scenario_type": item["testType"] if mode == "ai_bot" else "",
                   "test_scenario": item["title"] if mode == "ai_bot" else "",
                   "input_example": item["inputExample"] if mode == "ai_bot" else "",
                   "check_focus": item["checkFocus"] if mode == "ai_bot" else "",
                   "expected_result": item["expectedBehavior"] if mode == "ai_bot" else "",
                   "bot_status": report.get("botAssessment", {}).get("status"),
                   "bot_rules_json": json.dumps(report.get("botAssessment", {}).get("rules", []), ensure_ascii=False),
                   "artifact_filename": (item["artifact"] or {}).get("filename"),
                   "artifact_format": (item["artifact"] or {}).get("format"),
                   "artifact_size": (item["artifact"] or {}).get("size"),
                   "artifact_sha256": (item["artifact"] or {}).get("sha256"),
                   "artifact_text_truncated": (item["artifact"] or {}).get("textTruncated"),
                   "response_id": item["responseId"], "model": item["model"],
                   "rubric_version": item["rubricVersion"], "score_type": report.get("scoreType", "legacy"),
                   "evaluator_model": item["evaluatorModel"], "evaluated_at": item["evaluatedAt"],
                   "stale": item["stale"], "needs_review": report.get("needsReview"), "input_hash": report.get("inputHash"),
                   "quality_violations_json": json.dumps({key: axis.get("violations", []) for key, axis in report.get("axes", {}).items()}, ensure_ascii=False),
                   "quality_pending_json": json.dumps({key: axis.get("pendingReasons", []) for key, axis in report.get("axes", {}).items()}, ensure_ascii=False),
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
