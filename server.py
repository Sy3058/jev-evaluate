#!/usr/bin/env python3
"""Local Korean chatbot response evaluator backed by TypeSafe Jev."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DB = ROOT / "data" / "evaluations.db"
DEFAULT_ENV = ROOT / ".env"
API_URL = "https://api.typesafe.ai/v1/systemone"
MODELS = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "claude-sonnet-5",
    "claude-opus-5",
    "gemini-3.5-flash",
]

QUALITY_QUESTIONS = {
    "instruction_following": {
        "type": "noul",
        "instructions": "사용자의 명시적인 요구사항, 제약, 요청 형식을 모두 충족했는가? 대화에 드러나지 않은 시스템 지침은 추측하지 않는다.",
    },
    "relevance": {
        "type": "noul",
        "instructions": "답변이 사용자의 질문과 목적에 직접 집중하며 관련 없는 내용으로 흐르지 않는가?",
    },
    "completeness": {
        "type": "noul",
        "instructions": "답변이 질문을 해결하는 데 필요한 핵심 요소를 빠뜨리지 않았는가? 알 수 없는 정보를 억지로 채우는 것은 완전성으로 보지 않는다.",
    },
    "apparent_correctness": {
        "type": "noul",
        "instructions": "제공된 대화와 첨부자료만을 기준으로 볼 때 답변에 명백한 사실 오류, 논리 오류 또는 자기모순이 없는가? 외부 근거가 없으면 확인 불가능한 사실을 맞다고 단정하지 않는다.",
    },
    "clarity_style": {
        "type": "noul",
        "instructions": "답변이 한국어로 명확하고 읽기 쉬우며 구조가 적절하고 불필요하게 장황하지 않은가?",
    },
    "overall_quality": {
        "type": "noul",
        "instructions": "종합적으로 이 답변은 사용자의 목적을 실질적으로 달성하며 그대로 사용하기에 충분히 좋은가?",
    },
    "unsupported_claim_risk": {
        "type": "noul",
        "instructions": "답변에 외부 검증이 필요한 구체적 사실을 근거 없이 확신하여 단정하는 위험이 있는가? 일반적 조언이나 명시적인 불확실성 표시는 제외한다.",
    },
    "fabrication_risk": {
        "type": "noul",
        "instructions": "답변이 존재하지 않을 가능성이 큰 출처, 인용, API, 기능, 수치 또는 사건을 만들어낸 징후가 있는가?",
    },
    "critical_failure": {
        "type": "noul",
        "instructions": "답변에 사용 목적을 달성할 수 없게 만드는 치명적인 오류, 핵심 요구 누락, 위험한 지침 또는 심각한 오해가 있는가?",
    },
}

MAX_ANSWER_LINES = 30
MAX_EVIDENCE_LINES = 120
MAX_EVIDENCE_CANDIDATES = 6


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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
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
        columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
        if "evidence_json" not in columns:
            db.execute("ALTER TABLE evaluations ADD COLUMN evidence_json TEXT")


def numbered_lines(text: str, prefix: str, limit: int) -> dict[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {f"{prefix}{index}": line for index, line in enumerate(lines[:limit], 1)}


def search_terms(text: str) -> set[str]:
    words = re.findall(r"[가-힣A-Za-z0-9_]+", text.lower())
    terms = set(words)
    for word in words:
        if len(word) >= 3:
            terms.update(f"#{word[index:index + 2]}" for index in range(len(word) - 1))
    return terms


def retrieve_evidence_candidates(
    answer_lines: dict[str, str], evidence_lines: dict[str, str], limit: int
) -> dict[str, list[str]]:
    if not answer_lines or not evidence_lines:
        return {}
    evidence_terms = {key: search_terms(text) for key, text in evidence_lines.items()}
    document_frequency: dict[str, int] = {}
    for terms in evidence_terms.values():
        for term in terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    document_count = len(evidence_lines)
    candidates: dict[str, list[str]] = {}
    for answer_id, answer_text in answer_lines.items():
        answer_terms = search_terms(answer_text)
        ranked = []
        for evidence_id, terms in evidence_terms.items():
            shared = answer_terms & terms
            if not shared:
                continue
            weighted_overlap = sum(
                math.log((document_count + 1) / (document_frequency[term] + 0.5)) + 1
                for term in shared
            )
            normalization = math.sqrt(max(len(answer_terms), 1) * max(len(terms), 1))
            ranked.append((weighted_overlap / normalization, evidence_id))
        ranked.sort(key=lambda item: (-item[0], int(item[1][1:])))
        candidates[answer_id] = [evidence_id for _, evidence_id in ranked[:limit]]
    return candidates


def evidence_questions(
    answer_lines: dict[str, str],
    evidence_lines: dict[str, str],
    candidates: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    if not answer_lines or not evidence_lines:
        return {}
    questions: dict[str, dict[str, Any]] = {}
    for answer_id in answer_lines:
        answer_candidates = candidates.get(answer_id, [])
        if not answer_candidates:
            continue
        choices = {"NONE": "후보 중 답변 줄을 직접 뒷받침하거나 반박하는 근거가 없음"}
        for evidence_id in answer_candidates:
            choices[f"SUPPORTED__{evidence_id}"] = (
                f"candidate_evidence_lines의 {evidence_id} 원문이 답변 줄을 직접 뒷받침함"
            )
            choices[f"CONTRADICTED__{evidence_id}"] = (
                f"candidate_evidence_lines의 {evidence_id} 원문이 답변 줄과 직접 충돌함"
            )
        questions[f"evidence_{answer_id}"] = {
            "type": "choice",
            "instructions": f"답변 줄 {answer_id}와 후보 근거만 비교하라. 가장 직접적인 관계 하나를 선택하고, 단순 주제 유사성만 있거나 답변 줄이 검증 가능한 주장이 아니면 NONE을 선택한다. 외부 지식은 사용하지 않는다.",
            "criteria": choices,
        }
    return questions


def case_state(case: sqlite3.Row, response: sqlite3.Row) -> dict[str, Any]:
    return {
        "evaluation_language": "한국어",
        "category": case["category"],
        "user_prompt": case["prompt"],
        "conversation_history": case["conversation_history"] or "없음",
        "attachment_text": case["attachment_text"] or "없음",
        "assistant_response": response["content"],
    }


def call_jev(
    state: dict[str, Any], model: str, questions: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("TYPESAFE_API_KEY 환경변수가 설정되지 않았습니다.")
    payload = json.dumps(
        {"model": model, "state": state, "questions": questions or QUALITY_QUESTIONS},
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


def extract_scores(result: dict[str, Any]) -> dict[str, float]:
    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("JEV 응답에 answers 객체가 없습니다.")
    scores: dict[str, float] = {}
    for key in QUALITY_QUESTIONS:
        answer = answers.get(key)
        if not isinstance(answer, dict) or not isinstance(answer.get("noul"), (int, float)):
            raise RuntimeError(f"JEV 응답의 {key} 판정 형식이 올바르지 않습니다.")
        scores[key] = round(float(answer["noul"]) * 100, 2)
    return scores


def extract_evidence(
    result: dict[str, Any], answer_lines: dict[str, str], evidence_lines: dict[str, str]
) -> dict[str, Any]:
    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("JEV 근거 응답에 answers 객체가 없습니다.")
    claims = []
    for answer_id, text in answer_lines.items():
        evidence_answer = answers.get(f"evidence_{answer_id}")
        if evidence_answer is None:
            claims.append(
                {
                    "answerId": answer_id,
                    "answerText": text,
                    "status": "NOT_ENOUGH_EVIDENCE",
                    "evidenceId": "NONE",
                    "evidenceText": None,
                    "confidence": None,
                }
            )
            continue
        if not isinstance(evidence_answer, dict):
            raise RuntimeError(f"JEV 근거 응답의 {answer_id} 선택 형식이 올바르지 않습니다.")
        choice = evidence_answer.get("choice")
        if choice == "NONE":
            status = "NOT_ENOUGH_EVIDENCE"
            evidence_id = "NONE"
        elif isinstance(choice, str) and "__" in choice:
            status, evidence_id = choice.split("__", 1)
        else:
            raise RuntimeError(f"JEV 근거 응답의 {answer_id} 선택 형식이 올바르지 않습니다.")
        if status not in {"SUPPORTED", "CONTRADICTED", "NOT_ENOUGH_EVIDENCE"}:
            raise RuntimeError(f"JEV 근거 응답의 {answer_id} 상태 판정 형식이 올바르지 않습니다.")
        if evidence_id != "NONE" and evidence_id not in evidence_lines:
            raise RuntimeError(f"JEV가 알 수 없는 근거 ID {evidence_id}를 반환했습니다.")
        claims.append(
            {
                "answerId": answer_id,
                "answerText": text,
                "status": status,
                "evidenceId": evidence_id,
                "evidenceText": evidence_lines.get(evidence_id),
                "confidence": evidence_answer.get("confidence"),
            }
        )
    return {
        "claims": claims,
        "answerLineCount": len(answer_lines),
        "evidenceLineCount": len(evidence_lines),
    }


class AppHandler(SimpleHTTPRequestHandler):
    db_path = DEFAULT_DB
    evaluator_model = "jev-latest"

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
                    "criteria": list(QUALITY_QUESTIONS),
                    "apiKeyConfigured": bool(os.environ.get("TYPESAFE_API_KEY", "").strip()),
                    "evaluatorModel": self.evaluator_model,
                }
            )
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
        if not isinstance(responses, dict):
            raise ValueError("모델 답변 형식이 올바르지 않습니다.")
        populated = {model: str(responses.get(model, "")).strip() for model in MODELS}
        populated = {model: content for model, content in populated.items() if content}
        if not populated:
            raise ValueError("최소 한 개의 모델 답변을 입력해 주세요.")
        timestamp = now_iso()
        with connect(self.db_path) as db:
            cursor = db.execute(
                "INSERT INTO cases(title, category, prompt, conversation_history, attachment_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (title, category, prompt, history, attachment, timestamp),
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

    def evaluate(self, response_id: int) -> dict[str, Any]:
        with connect(self.db_path) as db:
            response = db.execute("SELECT * FROM responses WHERE id = ?", (response_id,)).fetchone()
            if response is None:
                raise ValueError("답변을 찾을 수 없습니다.")
            case = db.execute("SELECT * FROM cases WHERE id = ?", (response["case_id"],)).fetchone()
            assert case is not None
            quality_state = case_state(case, response)
            answer_lines = numbered_lines(response["content"], "A", MAX_ANSWER_LINES)
            evidence_lines = numbered_lines(case["attachment_text"], "E", MAX_EVIDENCE_LINES)
            result = call_jev(quality_state, self.evaluator_model)
            scores = extract_scores(result)
            evidence = None
            evidence_result = None
            candidates = retrieve_evidence_candidates(
                answer_lines, evidence_lines, MAX_EVIDENCE_CANDIDATES
            )
            questions = evidence_questions(answer_lines, evidence_lines, candidates)
            if questions:
                candidate_ids = {
                    evidence_id
                    for answer_candidates in candidates.values()
                    for evidence_id in answer_candidates
                }
                evidence_state = {
                    "evaluation_language": "한국어",
                    "user_prompt": case["prompt"],
                    "numbered_answer_lines": answer_lines,
                    "candidate_evidence_lines": {
                        evidence_id: evidence_lines[evidence_id]
                        for evidence_id in evidence_lines
                        if evidence_id in candidate_ids
                    },
                }
                evidence_result = call_jev(evidence_state, self.evaluator_model, questions)
                evidence = extract_evidence(evidence_result, answer_lines, evidence_lines)
            cursor = db.execute(
                "INSERT INTO evaluations(response_id, evaluator_model, scores_json, evidence_json, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    response_id,
                    str(result.get("model", self.evaluator_model)),
                    json.dumps(scores, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False) if evidence else None,
                    json.dumps(
                        {"quality": result, "evidence": evidence_result}, ensure_ascii=False
                    ),
                    now_iso(),
                ),
            )
        return {
            "id": cursor.lastrowid,
            "responseId": response_id,
            "scores": scores,
            "evidence": evidence,
        }

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
            except RuntimeError as error:
                errors.append({"responseId": response_id, "error": str(error)})
                break
        return {"completed": len(completed), "remaining": len(rows) - len(completed), "errors": errors}

    def list_results(self) -> dict[str, Any]:
        with connect(self.db_path) as db:
            rows = db.execute(
                """
                SELECT c.id AS case_id, c.title, c.category, c.prompt,
                       r.id AS response_id, r.model, r.content,
                       e.id AS evaluation_id, e.evaluator_model, e.scores_json, e.evidence_json,
                       e.created_at AS evaluated_at
                FROM cases c
                JOIN responses r ON r.case_id = c.id
                LEFT JOIN evaluations e ON e.id = (
                    SELECT e2.id FROM evaluations e2
                    WHERE e2.response_id = r.id ORDER BY e2.id DESC LIMIT 1
                )
                ORDER BY c.id DESC, r.id ASC
                """
            ).fetchall()
        return {
            "items": [
                {
                    "caseId": row["case_id"],
                    "title": row["title"],
                    "category": row["category"],
                    "prompt": row["prompt"],
                    "responseId": row["response_id"],
                    "model": row["model"],
                    "content": row["content"],
                    "evaluationId": row["evaluation_id"],
                    "evaluatorModel": row["evaluator_model"],
                    "scores": json.loads(row["scores_json"]) if row["scores_json"] else None,
                    "evidence": json.loads(row["evidence_json"]) if row["evidence_json"] else None,
                    "evaluatedAt": row["evaluated_at"],
                }
                for row in rows
            ]
        }

    def export_csv(self) -> None:
        items = self.list_results()["items"]
        output = io.StringIO()
        fields = ["case_id", "title", "category", "prompt", "response_id", "model", *QUALITY_QUESTIONS]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in items:
            row = {
                "case_id": item["caseId"],
                "title": item["title"],
                "category": item["category"],
                "prompt": item["prompt"],
                "response_id": item["responseId"],
                "model": item["model"],
            }
            row.update(item["scores"] or {})
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
    parser.add_argument("--model", default="jev-latest")
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
