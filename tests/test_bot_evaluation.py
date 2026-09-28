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


def select_if(raw, slot, claim, requirement="R1", observation="O1", severity="MINOR",
              confidence=.95, target="BOTH"):
    for name, choice in (("claim", claim), ("requirement", requirement), ("applicability", "APPLIES"),
                         ("target", target), ("observation", observation), ("severity", severity)):
        raw["answers"][f"bot_if_{name}_{slot}"]["choice"] = choice
    raw["answers"][f"bot_if_claim_{slot}"]["confidence"] = confidence


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
            if any(key.startswith("bot_if_support_") for key in questions):
                return {"model": model, "answers": {key: {"choice": "SUPPORTED", "confidence": .95}
                                                       for key in questions}}
            self.assertEqual(state["check_focus"], "지침의 톤과 형식" if state["bot_test_type"] == "normal" else "역할 밖 조언 거절")
            self.assertNotIn("instruction_following", questions)
            self.assertIn("bot_if_claim_1", questions)
            self.assertIn("R1", questions["bot_if_requirement_1"]["criteria"])
            self.assertIn("O1", questions["bot_if_observation_1"]["criteria"])
            self.assertIn("bot_expected", questions)
            raw = bot_jev(state, model, questions, verdict="COMPLIANT" if state["bot_test_type"] == "normal" else "VIOLATED")
            if state["bot_test_type"] == "exception":
                raw["answers"]["bot_expected"]["choice"] = "VIOLATED"
                select_if(raw, 1, "ROLE_SCOPE", severity="MAJOR")
            return raw
        with patch("server.call_jev", side_effect=judge):
            normal_report = self.handler.evaluate(normal["responses"][0]["id"])["report"]
            exception_report = self.handler.evaluate(exception["responses"][0]["id"])["report"]
        self.assertEqual(normal_report["axes"]["instruction_following"]["score"], 3)
        self.assertEqual(exception_report["axes"]["instruction_following"]["score"], 1)
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
        self.assertTrue(state["file_delivery_missing"])
        self.assertTrue(state["file_output_required"])
        self.assertFalse(state["file_delivery_confirmed"])
        raw = {"answers": {"bot_B1": {"choice": "VIOLATED", "confidence": .95},
                           "bot_ref_B1": {"choice": "A1", "confidence": .95},
                           "bot_expected": {"choice": "VIOLATED", "confidence": .95},
                           "bot_expected_ref": {"choice": "A1", "confidence": .95}}}
        assessment = bot_evaluation.parse_result(raw, state)
        self.assertEqual(assessment["status"], "violation")
        self.assertEqual(assessment["rules"][0]["verdict"], "VIOLATED")
        self.assertEqual(assessment["expectedResult"]["verdict"], "VIOLATED")
        self.assertEqual(assessment["expectedResult"]["answerRef"], "FILE_ABSENT")

        plain_state = {"answer": {"A1": "문서 작성 방법을 안내합니다."}}
        bot_evaluation.add_questions(plain_state, {}, "HTML 파일을 생성한다.", expected_behavior="HTML 파일 생성")
        self.assertTrue(plain_state["file_delivery_missing"])
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

    def test_discrete_bot_if_does_not_request_probability_score(self):
        bot, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        def uncertain_judge(state, model, questions):
            raw = bot_jev(state, model, questions)
            self.assertNotIn("instruction_following", questions)
            return raw
        with patch("server.call_jev", side_effect=uncertain_judge):
            report = self.handler.evaluate(response_id)["report"]
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 3)
        self.assertEqual(axis["status"], "rated")
        self.assertEqual(axis["violations"], [])

    def test_discrete_bot_if_uses_distinct_evidenced_violations(self):
        case = {"category": "일반지식·설명", "prompt": "도움말", "conversation_history": "",
                "attachment_text": "도움말의 근거", "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "안녕하세요. 도움말입니다."})
        bot_evaluation.add_questions(state, questions, "- 먼저 인사한다.\n- 도움말을 제공한다.")

        def grade(*issues, second_ref="O1", source=True):
            raw = bot_jev(state, "jev-test-fixed", questions)
            mapping = {"MINOR_TONE": ("TONE", "MINOR"),
                       "MINOR_WRONG_FORMAT": ("FORMAT", "MINOR"),
                       "MAJOR_ROLE_BOUNDARY": ("ROLE_SCOPE", "MAJOR"),
                       "MAJOR_MISSING_OUTPUT": ("OMISSION", "MAJOR"),
                       "MAJOR_WRONG_FORMAT": ("FORMAT", "MAJOR")}
            for slot, issue in enumerate(issues, 1):
                claim, severity = mapping[issue]
                select_if(raw, slot, claim, requirement=(f"R{slot}" if source else "NONE"),
                          observation=("FULL_MESSAGE" if claim == "OMISSION" else second_ref if slot == 2 else "O1"),
                          severity=severity)
            report = rubric.parse_result(raw, state, questions)
            report["botAssessment"] = bot_evaluation.parse_result(raw, state)
            report["groundingIssue"] = "NONE"
            report["responseLengthIssue"] = "NONE"
            bot_evaluation.apply_discrete_quality(raw, state, report)
            self.assertEqual(report["scoreType"], "violation_count_0_3")
            return report["axes"]["instruction_following"]

        self.assertEqual(grade()["score"], 3)
        self.assertEqual(grade("MINOR_TONE")["score"], 2)
        self.assertEqual(grade("MINOR_TONE", "MINOR_WRONG_FORMAT")["score"], 1)
        self.assertEqual(grade("MAJOR_ROLE_BOUNDARY")["score"], 1)
        self.assertEqual(grade("MAJOR_ROLE_BOUNDARY", "MAJOR_MISSING_OUTPUT")["score"], 0)
        missing_second_ref = grade("MAJOR_MISSING_OUTPUT", "MAJOR_WRONG_FORMAT", second_ref="NONE")
        self.assertEqual(missing_second_ref["score"], 1)
        self.assertTrue(missing_second_ref["needsReview"])
        missing_source = grade("MAJOR_ROLE_BOUNDARY", source=False)
        self.assertEqual(missing_source["score"], 3)
        self.assertTrue(missing_source["needsReview"])
        self.assertEqual(grade("MINOR_TONE", "MINOR_TONE")["score"], 1)
        state["file_delivery_confirmed"] = True
        state["generated_artifact"] = {"filename": "report.html", "format": "html", "size": 100,
                                         "textAvailable": True, "textTruncated": False}
        state["response_message"] = "파일을 내려받으세요."
        state["output_format"] = "html"
        state["inspection"] = {"rendered": True, "observations": {
            "visibleText": "안녕하세요. 도움말입니다.", "markdownMarkerCandidates": []}}
        bot_evaluation.add_questions(state, questions, "- 먼저 인사한다.\n- 도움말을 제공한다.",
                                     artifact=state["generated_artifact"])
        verified_file = grade("MINOR_WRONG_FORMAT")
        self.assertEqual(verified_file["score"], 3)
        self.assertTrue(verified_file["rejectedFindings"])

    def test_discrete_if_uses_linked_second_candidate_without_confidence_cutoff(self):
        case = {"category": "강의·첨부자료", "prompt": "HTML로 리뷰해줘", "conversation_history": "",
                "attachment_text": "발화자 1 (00:20) 의견이 있으시면 말씀해 주세요.",
                "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "<html><body>리뷰</body></html>"})
        bot_evaluation.add_questions(state, questions, "# 3. 발화 인용\n- 발화를 원문대로 인용한다.\n- 타임스탬프를 표시한다.")
        self.assertEqual(questions["bot_if_unverifiable_reason"]["type"], "choice")
        self.assertNotIn("FILE_UNVERIFIED", questions["bot_if_unverifiable_reason"]["criteria"])
        raw = bot_jev(state, "jev-test-fixed", questions)
        select_if(raw, 2, "OMISSION", requirement="R1", observation="FULL_MESSAGE",
                  severity="MINOR", confidence=.22)
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 2)
        self.assertEqual(axis["violations"][0]["requirementRef"], "R1")
        self.assertEqual(axis["violations"][0]["observationRef"], "FULL_MESSAGE")
        self.assertEqual(axis["violations"][0]["confidence"], .22)
        self.assertTrue(axis["needsReview"])
        self.assertEqual(axis["pendingReasonChoices"], [])
        self.assertEqual(axis["judgeUnverifiableReason"]["code"], "NONE")

        select_if(raw, 1, "OMISSION", requirement="R1", observation="FULL_MESSAGE",
                  severity="MINOR", confidence=.19)
        raw["answers"]["bot_if_claim_2"]["choice"] = "NONE"
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 2)
        self.assertEqual(axis["violations"][0]["confidence"], .19)

    def test_if_uses_authored_requirement_and_visible_behavior_for_any_bot(self):
        case = {"category": "일반지식·설명", "prompt": "종목을 추천해줘", "conversation_history": "",
                "attachment_text": "", "evaluation_spec_json": '{"outputFormat":"html"}'}
        inspection = {"rendered": True, "observations": {"visibleText": "여행 안내\nABC 주식을 사세요."}}
        state, questions = rubric.prepare(case, {"content": "<html><body>ABC 주식을 사세요.</body></html>"}, inspection)
        bot_evaluation.add_questions(state, questions, "- 여행 정보만 안내한다.\n- 결과는 HTML로 작성한다.")
        self.assertIn("R1", state["bot_requirements"])
        self.assertEqual(state["if_observations"]["O2"], "ABC 주식을 사세요.")
        self.assertEqual(questions["bot_if_requirement_1"]["criteria"]["R1"], "state.bot_requirements.R1")
        self.assertEqual(questions["bot_if_observation_1"]["criteria"]["O2"], "state.if_observations.O2")
        raw = bot_jev(state, "jev-test-fixed", questions)
        select_if(raw, 1, "ROLE_SCOPE", requirement="R1", observation="O2",
                  severity="MAJOR", confidence=.12)
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 1)
        self.assertEqual(axis["violations"][0]["sourceText"], "- 여행 정보만 안내한다.")
        self.assertEqual(axis["violations"][0]["answerText"], "ABC 주식을 사세요.")
        self.assertNotIn("<html>", axis["violations"][0]["answerText"])

        support_state, support_questions = bot_evaluation.if_evidence_support_request(raw, state)
        self.assertIn("bot_if_support_1", support_questions)
        self.assertIn("ABC 주식을 사세요.", support_state["candidates"]["C1"]["observation"])
        raw["answers"]["bot_if_support_1"] = {"choice": "UNSUPPORTED", "confidence": .2}
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        bot_evaluation.apply_discrete_quality(raw, state, report)
        self.assertEqual(report["axes"]["instruction_following"]["score"], 3)
        self.assertTrue(report["axes"]["instruction_following"]["needsReview"])
        self.assertIn("EVIDENCE_NOT_DIRECT", report["axes"]["instruction_following"]["rejectedFindings"][0])
        raw["answers"].pop("bot_if_support_1")

        select_if(raw, 1, "OMISSION", requirement="R1", observation="O2",
                  severity="MINOR", confidence=.95)
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 3)
        self.assertTrue(axis["needsReview"])
        self.assertIn("OMISSION_NOT_VERIFIABLE", axis["rejectedFindings"][0])

    def test_if_keeps_chat_message_separate_from_generated_file_body(self):
        case = {"category": "일반지식·설명", "prompt": "보고서를 작성해줘",
                "conversation_history": "", "attachment_text": "", "evaluation_spec_json": '{"outputFormat":"html"}'}
        inspection = {"rendered": True, "observations": {"visibleText": "보고서 제목\n본문 내용"}}
        state, questions = rubric.prepare(case, {"content": "파일을 내려받으세요."}, inspection)
        state["response_message"] = "파일을 내려받으세요."
        state["generated_artifact"] = {"filename": "report.html", "format": "html", "size": 100,
                                         "textAvailable": True, "textTruncated": False}
        bot_evaluation.add_questions(state, questions, "- 최종 답변은 파일 링크만 제공한다.\n- HTML 본문에 제목을 포함한다.",
                                     artifact=state["generated_artifact"])
        self.assertIn("[채팅 답변]", state["if_observations"]["O1"])
        self.assertIn("[생성 파일 본문]", state["if_observations"]["O2"])
        self.assertIn("FULL_MESSAGE", questions["bot_if_observation_1"]["criteria"])
        self.assertIn("FULL_ARTIFACT", questions["bot_if_observation_1"]["criteria"])
        raw = bot_jev(state, "jev-test-fixed", questions)
        select_if(raw, 1, "CONTRADICTION", requirement="R1", observation="O2",
                  severity="MINOR", target="MESSAGE")
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        axis = report["axes"]["instruction_following"]
        self.assertEqual(axis["score"], 3)
        self.assertTrue(axis["needsReview"])
        self.assertEqual(axis["pendingReasonChoices"], [])
        self.assertIn("OUTPUT_CHANNEL_MISMATCH", axis["rejectedFindings"][0])

    def test_length_without_visible_issue_rejects_unsupported_deduction(self):
        _, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        def judge(state, model, questions):
            raw = bot_jev(state, model, questions)
            raw["answers"]["bot_issue_response_length_1"]["choice"] = "MAJOR_REPETITION"
            raw["answers"]["bot_length_issue"]["choice"] = "NONE"
            return raw
        with patch("server.call_jev", side_effect=judge):
            report = self.handler.evaluate(response_id)["report"]
        self.assertEqual(report["responseLengthIssue"], "NONE")
        self.assertEqual(report["axes"]["response_length"]["score"], 3.0)
        self.assertTrue(report["axes"]["response_length"]["rejectedFindings"])

    def test_web_link_sources_and_independent_axis_grades(self):
        case = {"category": "일반지식·설명", "prompt": "최신 팁을 알려줘", "conversation_history": "",
                "attachment_text": "", "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "다음 팁을 참고하세요. https://example.org/report"})
        links = {"items": [{"url": "https://example.org/report", "finalUrl": "https://example.org/report",
                            "status": "verified", "httpStatus": 200, "contentType": "text/html",
                            "title": "퍼실리테이션 보고서", "excerpt": "회의에서 모두의 발언 기회를 확보합니다.",
                            "checkedAt": "2026-09-28T00:00:00+00:00", "sha256": "a" * 64,
                            "reason": None, "claimContext": "다음 팁을 참고하세요."}], "skippedCount": 0}
        from server import add_web_sources
        add_web_sources(state, questions, links)
        self.assertEqual(state["sources"]["E1"], "회의에서 모두의 발언 기회를 확보합니다.")
        self.assertIn("E1", questions["source_ref_truthfulness"]["criteria"])
        bot_evaluation.add_questions(state, questions, "녹취를 분석한 HTML 리포트만 작성한다.")
        self.assertIn("verified_cited_web_page", questions["status_truthfulness"]["instructions"])

        raw = bot_jev(state, "jev-test-fixed", questions, verdict="VIOLATED")
        select_if(raw, 1, "ROLE_SCOPE", requirement="R1", observation="O1", severity="MAJOR")
        select_if(raw, 2, "ROLE_SCOPE", requirement="R1", observation="O1", severity="MINOR")
        raw["answers"]["bot_length_issue"]["choice"] = "UNKNOWN"
        raw["answers"]["status_truthfulness"]["choice"] = "UNKNOWN"
        raw["answers"]["bot_issue_truthfulness_1"]["choice"] = "UNKNOWN"
        raw["answers"]["bot_issue_truthfulness_2"]["choice"] = "UNKNOWN"
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "UNKNOWN"
        report["responseLengthIssue"] = "UNKNOWN"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        self.assertEqual(report["axes"]["instruction_following"]["score"], 1)
        self.assertEqual(report["axes"]["instruction_following"]["violations"][0]["answerRef"], "O1")
        self.assertEqual(report["axes"]["response_length"]["score"], 3)
        self.assertIsNone(report["axes"]["truthfulness"]["score"])

    def test_cited_link_check_is_saved_in_bot_report(self):
        _, case = self.create_bot_case()
        response_id = case["responses"][0]["id"]
        observed = {}
        links = {"items": [{"url": "https://example.org/report", "finalUrl": "https://example.org/report",
                            "status": "verified", "httpStatus": 200, "contentType": "text/html",
                            "title": "보고서", "excerpt": "회의 참석자에게 고르게 발언 기회를 제공합니다.",
                            "checkedAt": "2026-09-28T00:00:00+00:00", "sha256": "a" * 64,
                            "reason": None, "claimContext": "발언 기회"}], "skippedCount": 0}
        def judge(state, model, questions):
            observed["sources"] = dict(state["sources"])
            observed["metadata"] = dict(state["source_metadata"])
            return bot_jev(state, model, questions)
        with (patch("server.check_answer_links", return_value=links),
              patch("server.call_jev", side_effect=judge)):
            report = self.handler.evaluate(response_id)["report"]
        self.assertEqual(report["linkVerification"]["items"][0]["sourceRef"], "E1")
        self.assertEqual(observed["sources"]["E1"], links["items"][0]["excerpt"])
        self.assertEqual(observed["metadata"]["E1"]["kind"], "verified_cited_web_page")
        self.assertIn("linkCheckMs", report["timings"])

    def test_partial_web_page_keeps_truth_score_reviewable(self):
        case = {"category": "일반지식·설명", "prompt": "팁을 알려줘", "conversation_history": "",
                "attachment_text": "", "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "회의 목표를 정합니다. https://example.org/report"})
        from server import add_web_sources
        add_web_sources(state, questions, {"items": [{"url": "https://example.org/report",
            "finalUrl": "https://example.org/report", "status": "verified", "title": "보고서",
            "excerpt": "회의 목표를 정합니다.", "checkedAt": "2026-09-28T00:00:00+00:00",
            "truncated": True}], "skippedCount": 0})
        bot_evaluation.add_questions(state, questions, "회의를 안내한다.")
        raw = bot_jev(state, "jev-test-fixed", questions)
        raw["answers"]["status_truthfulness"]["choice"] = "RATED"
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "NONE"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
        self.assertEqual(report["axes"]["truthfulness"]["score"], 3)
        self.assertTrue(report["axes"]["truthfulness"]["needsReview"])
        self.assertIn("일부 인용 링크", " ".join(report["axes"]["truthfulness"]["notes"]))

    def test_grounding_treats_document_label_as_metadata_and_requires_answer_evidence(self):
        case = {"category": "강의·첨부자료", "prompt": "리뷰해줘", "conversation_history": "",
                "attachment_text": "AI로 생성된 콘텐츠입니다\n\n발화자 1 (00:01)\n회의를 시작합니다.",
                "evaluation_spec_json": "{}"}
        state, questions = rubric.prepare(case, {"content": "발화자 1은 회의를 시작했습니다."})
        bot_evaluation.add_questions(state, questions, "회의록에 근거해 답한다.")
        self.assertIn("E1", questions["source_ref_truthfulness"]["criteria"])
        self.assertIn("업로드 자료 관련 주장", questions["bot_issue_truthfulness_1"]["instructions"])
        raw = bot_jev(state, "jev-test-fixed", questions)
        raw["answers"]["source_ref_truthfulness"]["choice"] = "E2"
        raw["answers"]["answer_ref_truthfulness"]["choice"] = "NONE"
        raw["answers"]["bot_issue_truthfulness_1"]["choice"] = "MINOR_CONTRADICTION"
        report = rubric.parse_result(raw, state, questions)
        report["botAssessment"] = bot_evaluation.parse_result(raw, state)
        report["groundingIssue"] = "INVENTED_EVENT"
        report["responseLengthIssue"] = "NONE"
        bot_evaluation.apply_discrete_quality(raw, state, report)
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
        with (patch("server.inspect_html", return_value={"rendered": False, "reason": "테스트"}),
              patch("server.call_jev", side_effect=bot_jev)):
            report = self.handler.evaluate(case["responses"][0]["id"])["report"]
        self.assertEqual(report["groundingIssue"], "NONE")
        self.assertEqual(report["quoteObservations"]["exactMatches"][0]["sourceRef"], "E1")
        self.assertEqual(report["axes"]["truthfulness"]["score"], 3)

    def test_missing_required_file_is_a_confirmed_violation(self):
        bot = self.handler.create_bot({"name": "리포트 Bot", "description": "HTML 리포트",
                                       "instructions": "HTML 파일을 생성한다."})
        case = self.handler.create_case({"title": "코칭 html 생성", "category": "강의·첨부자료",
                                         "prompt": "리뷰해줘", "evaluationMode": "ai_bot", "botId": bot["id"],
                                         "checkFocus": "파일 생성 여부", "expectedBehavior": "지침에 맞춰 HTML 파일 생성",
                                         "evaluationSpec": {"outputFormat": "html"},
                                         "responses": {"gpt-5.6-sol": '<!DOCTYPE html><html lang="ko"><head><title>리포트</title></head><body>코칭</body></html>'}})
        def judge(state, model, questions):
            raw = bot_jev(state, model, questions, verdict="VIOLATED")
            raw["answers"]["source_ref_instruction_following"]["choice"] = "B1"
            raw["answers"]["bot_expected"]["choice"] = "VIOLATED"
            return raw
        with (patch("server.inspect_html", return_value={"rendered": False, "reason": "테스트"}),
              patch("server.call_jev", side_effect=judge)):
            report = self.handler.evaluate(case["responses"][0]["id"])["report"]
        self.assertEqual(report["botAssessment"]["status"], "violation")
        self.assertEqual(report["botAssessment"]["expectedResult"]["verdict"], "VIOLATED")
        self.assertEqual(report["axes"]["instruction_following"]["score"], 1)
        self.assertEqual(report["axes"]["instruction_following"]["violations"][0]["observationRef"], "FILE_ABSENT")

    def test_large_bot_request_is_split_without_losing_answers(self):
        from server import call_bot_jev
        state = {"sources": {"E1": "자료" * 10000}, "bot_requirements": {"R1": "지침"},
                 "if_observations": {"O1": "답변"}}
        questions = {"status_instruction_following": {"type": "choice", "instructions": "가" * 8000,
                                                      "criteria": {"RATED": "평가"}},
                     "bot_B1": {"type": "choice", "instructions": "나" * 8000,
                                "criteria": {"COMPLIANT": "준수"}},
                     "status_truthfulness": {"type": "choice", "instructions": "다" * 8000,
                                             "criteria": {"RATED": "평가"}}}
        calls = []
        def judge(batch_state, model, batch_questions):
            calls.append((batch_state, batch_questions))
            return {"model": model, "answers": {key: {"choice": "RATED"} for key in batch_questions},
                    "usage": {"input_tokens": 10, "output_tokens": 2}}
        with patch("server.call_jev", side_effect=judge):
            result = call_bot_jev(state, "jev-test", questions)
        self.assertEqual(len(calls), 3)
        self.assertEqual(set(result["answers"]), set(questions))
        self.assertEqual(result["usage"]["input_tokens"], 30)
        self.assertNotIn("bot_requirements", calls[1][0])

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
