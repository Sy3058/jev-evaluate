import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_evaluation
import evaluation as rubric
from server import AppHandler, connect, init_db
from test_evaluation import fake_jev


def bot_jev(state, model, questions, verdict="COMPLIANT"):
    raw = fake_jev(state, model, questions)
    for key in questions:
        if key.startswith("bot_B"):
            raw["answers"][key] = {"choice": verdict, "confidence": .95}
        elif key.startswith("bot_ref_B"):
            raw["answers"][key] = {"choice": "A1", "confidence": .95}
        elif key == "bot_expected":
            raw["answers"][key] = {"choice": "COMPLIANT", "confidence": .95}
        elif key == "bot_expected_ref":
            raw["answers"][key] = {"choice": "A1", "confidence": .95}
    return raw


class BotEvaluationTests(unittest.TestCase):
    def test_instruction_item_limit_is_20000_characters(self):
        self.assertEqual(bot_evaluation.instruction_parts("가" * 20000), ["가" * 20000])
        with self.assertRaisesRegex(ValueError, "20000자"):
            bot_evaluation.instruction_parts("가" * 20001)
        bot = self.handler.create_bot({"name": "긴 지침 Bot", "description": "길이 확인",
                                       "instructions": "가" * 20000})
        self.assertEqual(bot["version"], 1)

    def test_heading_without_rule_is_context_for_next_section(self):
        parts = bot_evaluation.instruction_parts("# 7. 파일 생성\n\n## 7.1 산출물\n\nHTML 파일을 만든다.\n\n# 8. 점검\n\n파일을 확인한다.")
        self.assertEqual(len(parts), 2)
        self.assertIn("# 7. 파일 생성", parts[0])
        self.assertIn("HTML 파일을 만든다.", parts[0])

    def test_quote_observations_match_transcript_text(self):
        inspection = {"blocks": [{"context": ["html", "body", "blockquote"],
                                   "text": "“그 의견을 정리해 보겠습니다.”"},
                                  {"context": ["html", "body", "blockquote", "span"],
                                   "text": "[발화자 1] (00:20)"}]}
        observed = bot_evaluation.quote_observations(inspection, {"E2": "그 의견을 정리해 보겠습니다."})
        self.assertEqual(observed["exactMatches"][0]["sourceRef"], "E2")
        self.assertEqual(observed["unmatchedQuotes"], [])

    def test_markdown_policy_with_many_rules_is_preserved(self):
        instructions = "# 역할\n전문 코치로 평가한다.\n\n# 1. 평가 원칙\n" + "\n".join(
            f"- 원칙 {number}: 녹취 근거를 확인한다." for number in range(1, 51)) + "\n\n## 1.1 최종 산출물\n- HTML 파일을 만든다."
        parts = bot_evaluation.instruction_parts(instructions)
        self.assertLessEqual(len(parts), 30)
        self.assertIn("원칙 50", "\n".join(parts))
        self.assertIn("HTML 파일을 만든다.", "\n".join(parts))
        bot = self.handler.create_bot({"name": "퍼실리테이션 Bot", "description": "녹취 평가",
                                       "instructions": instructions})
        saved = next(item for item in self.handler.list_bots()["items"] if item["id"] == bot["id"])
        self.assertEqual(saved["instructions"], instructions)
        plain_list = "\n".join(f"- 규칙 {number}" for number in range(50))
        self.assertLessEqual(len(bot_evaluation.instruction_parts(plain_list)), 30)

    def test_long_answer_is_not_repeated_in_reference_choices(self):
        case = {"category": "일반지식·설명", "prompt": "설명해줘", "conversation_history": "",
                "attachment_text": "근거 내용" * 1000, "evaluation_spec_json": "{}"}
        answer = "답변 내용" * 1500
        state, questions = rubric.prepare(case, {"content": answer})
        bot_evaluation.add_questions(state, questions, "\n\n".join(f"규칙 {number}" for number in range(15)))
        reference_questions = (question for key, question in questions.items()
                               if key.startswith(("answer_ref_", "source_ref_", "bot_ref_")))
        self.assertTrue(all(all(len(str(choice)) < 100 for choice in question["criteria"].values())
                            for question in reference_questions))
        self.assertLess(len(json.dumps({"state": state, "questions": questions}, ensure_ascii=False)), 100000)

    def test_existing_database_is_classified_as_model_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            with connect(path) as db:
                db.executescript("""
                    CREATE TABLE cases (id INTEGER PRIMARY KEY, title TEXT, category TEXT, prompt TEXT,
                        conversation_history TEXT DEFAULT '', attachment_text TEXT DEFAULT '', created_at TEXT);
                    CREATE TABLE responses (id INTEGER PRIMARY KEY, case_id INTEGER, model TEXT, content TEXT, created_at TEXT);
                    CREATE TABLE evaluations (id INTEGER PRIMARY KEY, response_id INTEGER, evaluator_model TEXT,
                        scores_json TEXT, raw_json TEXT, created_at TEXT);
                    INSERT INTO cases VALUES (1, '기존', '일반지식·설명', '질문', '', '', 'past');
                    INSERT INTO responses VALUES (1, 1, 'gpt-5.6-sol', '답변', 'past');
                    INSERT INTO evaluations VALUES (1, 1, 'old', '{"score": 2}', '{}', 'past');
                """)
            init_db(path)
            with connect(path) as db:
                row = db.execute("SELECT evaluation_mode, bot_id, bot_version FROM cases WHERE id = 1").fetchone()
                self.assertEqual(tuple(row), ("model", None, None))
                self.assertEqual(db.execute("SELECT scores_json FROM evaluations WHERE id = 1").fetchone()[0], '{"score": 2}')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.handler = object.__new__(AppHandler)
        self.handler.db_path = Path(self.temp.name) / "test.db"
        init_db(self.handler.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def create_bot_case(self):
        bot = self.handler.create_bot({"name": "상담 Bot", "description": "정해진 양식으로 상담",
                                       "instructions": "- 먼저 인사한다.\n- 답변은 한국어로 작성한다."})
        case = self.handler.create_case({"title": "인사", "category": "일반지식·설명",
                                         "prompt": "도움말을 알려줘", "evaluationMode": "ai_bot",
                                         "botId": bot["id"], "responses": {"gpt-5.6-sol": "안녕하세요. 도움말입니다."}})
        return bot, case

    def test_model_migration_and_mode_isolation(self):
        with connect(self.handler.db_path) as db:
            db.execute("INSERT INTO cases(title, category, prompt, created_at) VALUES ('기존', '일반지식·설명', '질문', 'past')")
        self.assertEqual(self.handler.list_results()["items"], [])  # 답변 없는 케이스는 결과에 없다.
        model = self.handler.create_case({"title": "모델", "category": "일반지식·설명",
                                          "prompt": "질문", "responses": {"gpt-5.6-sol": "답변"}})
        bot, case = self.create_bot_case()
        self.assertEqual([i["caseId"] for i in self.handler.list_results("model")["items"]], [model["id"]])
        result = self.handler.list_results("ai_bot")["items"][0]
        self.assertEqual(result["caseId"], case["id"])
        self.assertEqual(result["botVersion"], 1)
        self.assertEqual(result["botId"], bot["id"])
        self.assertEqual(result["botInstructions"], "- 먼저 인사한다.\n- 답변은 한국어로 작성한다.")

    def test_instruction_priority_version_and_csv(self):
        bot, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        with patch("server.call_jev", side_effect=lambda *args: bot_jev(*args, verdict="VIOLATED")):
            report = self.handler.evaluate(response_id)["report"]
        self.assertEqual(report["botAssessment"]["status"], "violation")
        self.assertEqual(report["botAssessment"]["rules"][0]["answerRef"], "A1")
        self.assertIsNone(report["axes"]["instruction_following"]["score"])
        self.assertTrue(report["needsReview"])
        self.assertFalse(self.handler.list_results("ai_bot")["items"][0]["stale"])
        with patch.object(bot_evaluation, "QUALITY_VERSION", "future-bot-rubric"):
            stale = self.handler.list_results("ai_bot")["items"][0]
            self.assertTrue(stale["stale"])
            self.assertIn("평가 기준 변경", stale["staleReasons"])
        self.handler.update_bot(bot["id"], {"name": "상담 Bot", "description": "새 설명",
                                            "instructions": "이제 한 문장으로 답한다."})
        old = self.handler.list_results("ai_bot")["items"][0]
        self.assertEqual(old["botVersion"], 1)
        self.assertFalse(old["stale"])
        self.assertEqual(old["botInstructions"], "- 먼저 인사한다.\n- 답변은 한국어로 작성한다.")
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = lambda *args: None
        self.handler.send_header = lambda *args: None
        self.handler.end_headers = lambda: None
        self.handler.export_csv("ai_bot")
        row = next(csv.DictReader(io.StringIO(self.handler.wfile.getvalue().decode("utf-8-sig"))))
        self.assertEqual(row["bot_status"], "violation")
        self.assertEqual(json.loads(row["bot_rules_json"])[0]["verdict"], "VIOLATED")
        self.handler.update_case_bot_version(case["id"], {})
        self.assertTrue(self.handler.list_results("ai_bot")["items"][0]["stale"])
        self.assertEqual(self.handler.history(response_id)["items"][0]["report"]["botVersion"], 1)

    def test_normal_and_exception_scenarios_drive_bot_if(self):
        bot = self.handler.create_bot({"name": "퍼실리테이션 Bot", "description": "교육 리포트 전용",
                                       "instructions": "퍼실리테이션 리포트만 작성한다. 다른 분야의 조언은 거절한다."})
        normal = self.handler.create_case({"title": "리포트 작성", "category": "일반지식·설명",
                                           "prompt": "녹취를 평가해줘", "evaluationMode": "ai_bot", "botId": bot["id"],
                                           "testType": "normal", "inputExample": "녹취록을 업로드한다",
                                           "checkFocus": "지침의 톤과 형식", "expectedBehavior": "지침에 정의된 톤/형식으로 정확히 응답",
                                           "responses": {"gpt-5.6-sol": "교육 피드백입니다."}})
        exception = self.handler.create_case({"title": "주식 질문", "category": "일반지식·설명",
                                              "prompt": "주식 어떤 거 살까?", "evaluationMode": "ai_bot", "botId": bot["id"],
                                              "testType": "exception", "checkFocus": "역할 밖 조언 거절",
                                              "expectedBehavior": "주식 추천을 거절하고 퍼실리테이션 지원 범위를 안내",
                                              "responses": {"gpt-5.6-sol": "A 주식을 사세요."}})
        def judge(state, model, questions):
            self.assertEqual(state["check_focus"], "지침의 톤과 형식" if state["bot_test_type"] == "normal" else "역할 밖 조언 거절")
            self.assertIn("state.check_focus", questions["instruction_following"]["instructions"])
            self.assertIn("bot_expected", questions)
            raw = bot_jev(state, model, questions, verdict="COMPLIANT" if state["bot_test_type"] == "normal" else "VIOLATED")
            if state["bot_test_type"] == "exception":
                raw["answers"]["instruction_following"].update(score=0.0,
                    probabilities={"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0})
                raw["answers"]["bot_expected"]["choice"] = "VIOLATED"
            return raw
        with patch("server.call_jev", side_effect=judge):
            normal_report = self.handler.evaluate(normal["responses"][0]["id"])["report"]
            exception_report = self.handler.evaluate(exception["responses"][0]["id"])["report"]
        self.assertEqual(normal_report["axes"]["instruction_following"]["score"], 3)
        self.assertEqual(exception_report["axes"]["instruction_following"]["score"], 0)
        self.assertEqual(exception_report["botAssessment"]["expectedResult"]["verdict"], "VIOLATED")
        result = next(item for item in self.handler.list_results("ai_bot")["items"] if item["caseId"] == normal["id"])
        self.assertEqual(result["checkFocus"], "지침의 톤과 형식")
        self.assertEqual(result["inputExample"], "녹취록을 업로드한다")
        self.assertFalse(result["stale"])
        self.handler.update_settings(normal["id"], {"checkFocus": "인용 정확성",
                                                      "expectedBehavior": "원문을 정확히 인용"})
        self.assertTrue(next(item for item in self.handler.list_results("ai_bot")["items"] if item["caseId"] == normal["id"])["stale"])

    def test_exception_requires_expected_result(self):
        bot, _ = self.create_bot_case()
        with self.assertRaisesRegex(ValueError, "기대 행동"):
            self.handler.create_case({"title": "예외", "category": "일반지식·설명", "prompt": "다른 질문",
                                      "evaluationMode": "ai_bot", "botId": bot["id"], "testType": "exception",
                                      "responses": {"gpt-5.6-sol": "답변"}})

    def test_pasted_html_does_not_prove_file_creation(self):
        html = '<!DOCTYPE html><html lang="ko"><head><title>코칭</title></head><body>리포트</body></html>'
        state = {"answer": {"A1": html}}
        questions = {}
        bot_evaluation.add_questions(state, questions, "HTML 파일을 생성한다.", expected_behavior="HTML 파일 생성",
                                     check_focus="파일 생성 여부")
        self.assertTrue(state["artifact_verification_pending"])
        self.assertFalse(state["file_delivery_confirmed"])
        raw = {"answers": {"bot_B1": {"choice": "VIOLATED", "confidence": .95},
                           "bot_ref_B1": {"choice": "A1", "confidence": .95},
                           "bot_expected": {"choice": "VIOLATED", "confidence": .95},
                           "bot_expected_ref": {"choice": "A1", "confidence": .95}}}
        assessment = bot_evaluation.parse_result(raw, state)
        self.assertEqual(assessment["status"], "review")
        self.assertEqual(assessment["rules"][0]["verdict"], "UNKNOWN")
        self.assertEqual(assessment["expectedResult"]["verdict"], "UNKNOWN")

        plain_state = {"answer": {"A1": "문서 작성 방법을 안내합니다."}}
        bot_evaluation.add_questions(plain_state, {}, "HTML 파일을 생성한다.", expected_behavior="HTML 파일 생성")
        self.assertFalse(plain_state["artifact_verification_pending"])
        self.assertEqual(bot_evaluation.parse_result(raw, plain_state)["status"], "violation")

    def test_uncertain_violation_stays_in_review(self):
        state = {"answer": {"A1": "주식 추천을 합니다."}}
        bot_evaluation.add_questions(state, {}, "주식 조언을 하지 않는다.", expected_behavior="주식 조언 거절")
        raw = {"answers": {"bot_B1": {"choice": "VIOLATED", "confidence": .42},
                           "bot_ref_B1": {"choice": "A1", "confidence": .95},
                           "bot_expected": {"choice": "VIOLATED", "confidence": .49},
                           "bot_expected_ref": {"choice": "A1", "confidence": .95}}}
        assessment = bot_evaluation.parse_result(raw, state)
        self.assertEqual(assessment["status"], "review")
        self.assertTrue(assessment["needsReview"])

    def test_low_confidence_bot_if_keeps_score_with_review(self):
        bot, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        def uncertain_judge(state, model, questions):
            raw = bot_jev(state, model, questions)
            raw["answers"]["instruction_following"]["confidence"] = .47
            raw["answers"]["instruction_following"]["score"] = 1.8
            raw["answers"]["instruction_following"]["probabilities"] = {"0": .0, "1": .3, "2": .6, "3": .1}
            return raw
        with patch("server.call_jev", side_effect=uncertain_judge):
            report = self.handler.evaluate(response_id)["report"]
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 1.8)
        self.assertEqual(axis["status"], "rated")
        self.assertTrue(axis["needsReview"])
        self.assertIn("확신도", " ".join(axis["notes"]))

    def test_length_without_visible_issue_is_not_fractionally_deducted(self):
        _, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        def judge(state, model, questions):
            raw = bot_jev(state, model, questions)
            raw["answers"]["response_length"].update(
                score=2.5, probabilities={"0": 0.0, "1": 0.0, "2": .5, "3": .5})
            raw["answers"]["bot_length_issue"]["choice"] = "NONE"
            return raw
        with patch("server.call_jev", side_effect=judge):
            report = self.handler.evaluate(response_id)["report"]
        self.assertEqual(report["responseLengthIssue"], "NONE")
        self.assertEqual(report["axes"]["response_length"]["score"], 3.0)
        self.assertEqual(report["axes"]["response_length"]["judgeScore"], 2.5)

    def test_grounding_treats_document_label_as_metadata_and_requires_answer_evidence(self):
        case = {"category": "강의·첨부자료", "prompt": "리뷰해줘", "conversation_history": "",
                "attachment_text": "AI로 생성된 콘텐츠입니다\n\n발화자 1 (00:01)\n회의를 시작합니다.",
                "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "발화자 1은 회의를 시작했습니다."})
        bot_evaluation.add_questions(state, questions, "회의록에 근거해 답한다.")
        self.assertIn("E1", questions["source_ref_truthfulness"]["criteria"])
        self.assertIn("제목·출처·작성 표기", questions["truthfulness"]["instructions"])
        raw = bot_jev(state, "jev-test-fixed", questions)
        raw["answers"]["source_ref_truthfulness"]["choice"] = "E2"
        raw["answers"]["answer_ref_truthfulness"]["choice"] = "NONE"
        report = rubric.parse_result(raw, state, questions)
        self.assertIsNone(report["axes"]["truthfulness"]["score"])
        self.assertEqual(report["axes"]["truthfulness"]["status"], "unverifiable")

    def test_exact_quote_and_no_grounding_issue_supports_bot_truthfulness(self):
        bot = self.handler.create_bot({"name": "코칭 Bot", "description": "녹취 리뷰",
                                       "instructions": "녹취 발화를 그대로 인용한다."})
        case = self.handler.create_case({"title": "리뷰", "category": "강의·첨부자료", "prompt": "리뷰해줘",
                                         "evaluationMode": "ai_bot", "botId": bot["id"],
                                         "attachmentText": "발화자 1 (00:01)\n그 의견을 정리해 보겠습니다.",
                                         "responses": {"gpt-5.6-sol": '<!DOCTYPE html><html lang="ko"><body><blockquote>“그 의견을 정리해 보겠습니다.”</blockquote></body></html>'},
                                         "evaluationSpec": {"outputFormat": "html"}})
        def judge(state, model, questions):
            raw = bot_jev(state, model, questions)
            raw["answers"]["truthfulness"].update(
                score=2.5, probabilities={"0": 0.0, "1": 0.0, "2": .5, "3": .5})
            return raw
        with (patch("server.inspect_html", return_value={"rendered": False, "reason": "테스트"}),
              patch("server.call_jev", side_effect=judge)):
            report = self.handler.evaluate(case["responses"][0]["id"])["report"]
        self.assertEqual(report["groundingIssue"], "NONE")
        self.assertEqual(report["quoteObservations"]["exactMatches"][0]["sourceRef"], "E1")
        self.assertEqual(report["axes"]["truthfulness"]["score"], 2.5)

    def test_file_only_if_deduction_is_withheld(self):
        bot = self.handler.create_bot({"name": "리포트 Bot", "description": "HTML 리포트",
                                       "instructions": "HTML 파일을 생성한다."})
        case = self.handler.create_case({"title": "코칭 html 생성", "category": "강의·첨부자료",
                                         "prompt": "리뷰해줘", "evaluationMode": "ai_bot", "botId": bot["id"],
                                         "checkFocus": "파일 생성 여부", "expectedBehavior": "지침에 맞춰 HTML 파일 생성",
                                         "evaluationSpec": {"outputFormat": "html"},
                                         "responses": {"gpt-5.6-sol": '<!DOCTYPE html><html lang="ko"><head><title>리포트</title></head><body>코칭</body></html>'}})
        def judge(state, model, questions):
            raw = bot_jev(state, model, questions, verdict="VIOLATED")
            raw["answers"]["instruction_following"].update(score=1.0,
                probabilities={"0": 0.0, "1": 1.0, "2": 0.0, "3": 0.0})
            raw["answers"]["source_ref_instruction_following"]["choice"] = "B1"
            raw["answers"]["bot_expected"]["choice"] = "VIOLATED"
            return raw
        with (patch("server.inspect_html", return_value={"rendered": False, "reason": "테스트"}),
              patch("server.call_jev", side_effect=judge)):
            report = self.handler.evaluate(case["responses"][0]["id"])["report"]
        self.assertEqual(report["botAssessment"]["status"], "review")
        self.assertIsNone(report["axes"]["instruction_following"]["score"])
        self.assertIn("실제 파일 생성 관찰 불가", report["issues"])

    def test_review_for_unknown_and_no_applicable_rules(self):
        state = {"answer": {"A1": "답변"}}
        questions = {}
        bot_evaluation.add_questions(state, questions, "한국어로 답한다.")
        for verdict in ("UNKNOWN", "PARTIAL", "NA"):
            raw = {"answers": {"bot_B1": {"choice": verdict, "confidence": .95},
                                "bot_ref_B1": {"choice": "A1", "confidence": .95}}}
            self.assertEqual(bot_evaluation.parse_result(raw, state)["status"], "review")
        missing_evidence = {"answers": {"bot_B1": {"choice": "VIOLATED", "confidence": .95},
                                        "bot_ref_B1": {"choice": "NONE", "confidence": .95}}}
        assessment = bot_evaluation.parse_result(missing_evidence, state)
        self.assertEqual(assessment["status"], "review")
        self.assertEqual(assessment["rules"][0]["judgeVerdict"], "VIOLATED")
        self.assertEqual(assessment["rules"][0]["verdict"], "UNKNOWN")
        safe_refusal = {"answers": {"bot_B1": {"choice": "VIOLATED", "confidence": .95},
                                    "bot_ref_B1": {"choice": "A1", "confidence": .95},
                                    "requirement_R1": {"choice": "SAFE_REFUSAL", "confidence": .95}}}
        assessment = bot_evaluation.parse_result(safe_refusal, state)
        self.assertEqual(assessment["status"], "review")
        self.assertIn("안전상", assessment["rules"][0]["reason"])

    def test_bot_filter_applies_to_pending_and_export(self):
        first_bot, first_case = self.create_bot_case()
        second_bot = self.handler.create_bot({"name": "다른 Bot", "description": "별도 지침",
                                              "instructions": "짧게 답한다."})
        second_case = self.handler.create_case({"title": "둘째", "category": "일반지식·설명",
                                                "prompt": "질문", "evaluationMode": "ai_bot", "botId": second_bot["id"],
                                                "responses": {"gpt-5.6-sol": "짧은 답"}})
        with patch("server.call_jev", side_effect=bot_jev):
            result = self.handler.evaluate_pending("ai_bot", first_bot["id"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.handler.history(first_case["responses"][0]["id"])["items"][0]["report"]["botVersion"], 1)
        self.assertEqual(self.handler.history(second_case["responses"][0]["id"])["items"], [])
        self.assertEqual(len(self.handler.list_results("ai_bot", first_bot["id"])["items"]), 1)
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = lambda *args: None
        self.handler.send_header = lambda *args: None
        self.handler.end_headers = lambda: None
        self.handler.export_csv("ai_bot", first_bot["id"])
        rows = list(csv.DictReader(io.StringIO(self.handler.wfile.getvalue().decode("utf-8-sig"))))
        self.assertEqual([row["bot_id"] for row in rows], [str(first_bot["id"])])
        with self.assertRaises(ValueError):
            self.handler.evaluate_pending("model", first_bot["id"])


if __name__ == "__main__":
    unittest.main()
