import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import (  # noqa: E402
    QUALITY_QUESTIONS,
    connect,
    evidence_questions,
    extract_evidence,
    extract_scores,
    init_db,
    load_env_file,
    numbered_lines,
    retrieve_evidence_candidates,
)


class DatabaseTests(unittest.TestCase):
    def test_init_db_creates_expected_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            init_db(path)
            with connect(path) as db:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"cases", "responses", "evaluations"}.issubset(names))

    def test_init_db_adds_evidence_column_to_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with connect(path) as db:
                db.execute(
                    """CREATE TABLE evaluations (
                        id INTEGER PRIMARY KEY,
                        response_id INTEGER NOT NULL,
                        evaluator_model TEXT NOT NULL,
                        scores_json TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
            init_db(path)
            with connect(path) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(evaluations)")}
            self.assertIn("evidence_json", columns)


class ScoreParsingTests(unittest.TestCase):
    def test_extract_scores_converts_probabilities_to_percent(self):
        result = {"answers": {key: {"type": "noul", "noul": 0.825} for key in QUALITY_QUESTIONS}}
        scores = extract_scores(result)
        self.assertEqual(scores["overall_quality"], 82.5)
        self.assertEqual(set(scores), set(QUALITY_QUESTIONS))

    def test_extract_scores_rejects_missing_answer(self):
        with self.assertRaisesRegex(RuntimeError, "판정 형식"):
            extract_scores({"answers": {}})

    def test_questions_are_json_serializable(self):
        self.assertIn("critical_failure", json.loads(json.dumps(QUALITY_QUESTIONS)))


class EnvironmentTests(unittest.TestCase):
    def test_load_env_file_reads_quoted_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('JEV_TEST_KEY="secret-value"\n', encoding="utf-8")
            os.environ.pop("JEV_TEST_KEY", None)
            try:
                load_env_file(path)
                self.assertEqual(os.environ["JEV_TEST_KEY"], "secret-value")
            finally:
                os.environ.pop("JEV_TEST_KEY", None)

    def test_load_env_file_does_not_override_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("JEV_TEST_KEY=file-value\n", encoding="utf-8")
            os.environ["JEV_TEST_KEY"] = "shell-value"
            try:
                load_env_file(path)
                self.assertEqual(os.environ["JEV_TEST_KEY"], "shell-value")
            finally:
                os.environ.pop("JEV_TEST_KEY", None)


class EvidenceTests(unittest.TestCase):
    def test_numbered_lines_ignores_blanks_and_keeps_text(self):
        self.assertEqual(
            numbered_lines("첫 줄\n\n 둘째 줄 ", "E", 10),
            {"E1": "첫 줄", "E2": "둘째 줄"},
        )

    def test_evidence_questions_offer_ids_and_none(self):
        questions = evidence_questions({"A1": "주장"}, {"E1": "근거"}, {"A1": ["E1"]})
        self.assertEqual(
            questions["evidence_A1"]["criteria"]["SUPPORTED__E1"],
            "candidate_evidence_lines의 E1 원문이 답변 줄을 직접 뒷받침함",
        )
        self.assertIn("NONE", questions["evidence_A1"]["criteria"])

    def test_retrieval_limits_candidates_and_prefers_overlap(self):
        candidates = retrieve_evidence_candidates(
            {"A1": "파이썬 비동기 이벤트 루프"},
            {
                "E1": "이벤트 루프는 비동기 작업을 관리한다.",
                "E2": "데이터베이스 인덱스 설명",
                "E3": "파이썬 코루틴과 await 설명",
            },
            2,
        )
        self.assertEqual(candidates["A1"], ["E1", "E3"])

    def test_evidence_questions_skip_lines_without_candidates(self):
        questions = evidence_questions({"A1": "제목"}, {"E1": "근거"}, {"A1": []})
        self.assertEqual(questions, {})

    def test_extract_evidence_maps_id_back_to_original_text(self):
        result = {
            "answers": {
                "evidence_A1": {
                    "type": "choice",
                    "choice": "SUPPORTED__E1",
                    "confidence": 0.7,
                },
            }
        }
        evidence = extract_evidence(result, {"A1": "주장"}, {"E1": "원문 근거"})
        self.assertEqual(evidence["claims"][0]["evidenceText"], "원문 근거")
        self.assertEqual(evidence["claims"][0]["status"], "SUPPORTED")

if __name__ == "__main__":
    unittest.main()
