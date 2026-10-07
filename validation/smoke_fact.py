"""Live JEV checks of claim-level Truthfulness without changing the app DB."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import AppHandler, call_jev, init_db, load_env_file


def main() -> None:
    load_env_file()
    with tempfile.TemporaryDirectory() as directory:
        handler = object.__new__(AppHandler)
        handler.db_path = Path(directory) / "smoke.db"
        init_db(handler.db_path)
        cases = [
            ("no_evidence", "일반지식·설명", "",
             "무료 요금제에서는 파일을 내보낼 수 있습니다.", "UNVERIFIED", None, False),
            ("supported", "일반지식·설명", "무료 요금제에서는 파일을 내보낼 수 없습니다.",
             "무료 요금제에서는 파일을 내보낼 수 없습니다.", "SUPPORTED", 3, False),
            ("contradicted", "일반지식·설명", "무료 요금제에서는 파일을 내보낼 수 없습니다.",
             "무료 요금제에서는 파일을 내보낼 수 있습니다.", "CONTRADICTED", 1, False),
            ("irrelevant_source", "일반지식·설명", "서울은 대한민국의 수도입니다.",
             "무료 요금제에서는 파일을 내보낼 수 있습니다.", "UNVERIFIED", None, False),
            ("document_supported", "강의·첨부자료", "행사는 월요일에 열립니다.",
             "행사는 월요일에 열립니다.", "SUPPORTED", 3, True),
            ("reasoning_contradicted", "추론·문제해결", "두 수의 합은 12입니다.",
             "두 수의 합은 13입니다.", "CONTRADICTED", 1, False),
            ("reasoning_short_number", "추론·문제해결", "4",
             "5", "CONTRADICTED", 1, False),
            ("paraphrase", "강의·첨부자료", "회의는 월요일 오후 2시에 시작합니다.",
             "회의 시작 시각은 월요일 14시입니다.", "SUPPORTED", 3, True),
            ("wrong_speaker", "강의·첨부자료", "민수가 발표했습니다. 지수는 기록했습니다.",
             "지수가 발표했습니다.", "CONTRADICTED", 1, True),
            ("invented_event", "강의·첨부자료", "행사는 월요일에 열립니다.",
             "행사에서 투표를 진행했습니다.", "UNVERIFIED", None, True),
            ("recommendation", "강의·첨부자료", "행사는 월요일에 열립니다.",
             "다음에는 투표를 진행하는 것이 좋겠습니다.", "NOT_APPLICABLE", None, True),
            ("unsupported_consensus", "강의·첨부자료", "참석자 3명이 동의했습니다.",
             "참석자 전원이 동의했습니다.", "UNVERIFIED", None, True),
            ("repeated_contradiction", "강의·첨부자료", "행사는 월요일에 열립니다.",
             "행사는 화요일에 열립니다. 행사는 화요일에 열립니다.", "CONTRADICTED", 1, True),
            ("partial_support", "일반지식·설명", "기본 요금제는 최대 10명까지 사용할 수 있습니다.",
             "기본 요금제는 최대 10명까지 사용할 수 있고 파일 내보내기도 지원합니다.", "UNVERIFIED", None, False),
            ("wrong_plan", "일반지식·설명", "A 요금제는 파일 내보내기를 지원합니다. B 요금제는 지원하지 않습니다.",
             "B 요금제는 파일 내보내기를 지원합니다.", "CONTRADICTED", 1, False),
            ("future_as_current", "일반지식·설명", "2027년에 파일 내보내기를 도입할 계획입니다.",
             "현재 파일 내보내기를 지원합니다.", "UNVERIFIED", None, False),
        ]
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--case", help="특정 진단 사례 이름만 실행")
        parser.add_argument("--repeats", type=int, default=1)
        args = parser.parse_args()
        if args.repeats < 1:
            parser.error("--repeats는 1 이상이어야 합니다.")
        selected = [case for case in cases if not args.case or case[0] == args.case]
        if not selected:
            parser.error("진단 사례 이름을 찾을 수 없습니다.")
        failures = []
        for name, category, source, answer, expected_relation, expected_score, use_attachment in selected * args.repeats:
            created = handler.create_case({
                "title": name,
                "category": category,
                "prompt": "제공된 근거에 따라 답해줘.",
                "attachmentText": source if use_attachment else "",
                "responses": {"gpt-5.6-sol": answer},
                "evaluationSpec": {"expectedAnswer": "" if use_attachment else source, "confirmed": True},
            })
            captured = {}
            def capture(*args, **kwargs):
                result = call_jev(*args, **kwargs)
                captured.update(result.get("answers", {}))
                return result
            try:
                with patch("server.call_jev", side_effect=capture):
                    report = handler.evaluate(created["responses"][0]["id"])["report"]
            except Exception as exc:
                scores = {key: {"score": value.get("score"), "probabilities": value.get("probabilities")}
                          for key, value in captured.items() if isinstance(value, dict) and "score" in value}
                print(json.dumps({"case": name, "error": str(exc), "scores": scores}, ensure_ascii=True), flush=True)
                failures.append(name)
                continue
            fact = report["factVerification"]
            relation = fact["claims"][0]["relation"]
            score = report["axes"]["truthfulness"]["score"]
            print(json.dumps({"case": name, "relation": relation, "score": score,
                              "importance": fact["claims"][0]["importance"],
                              "sourceRef": fact["claims"][0]["sourceRef"],
                              "unverifiedCount": fact["unverifiedCount"],
                              "relationConfidence": fact["claims"][0].get("relationConfidence"),
                              "needsReview": fact["needsReview"]}, ensure_ascii=True), flush=True)
            if (relation, score) != (expected_relation, expected_score):
                failures.append(name)
        if failures:
            raise RuntimeError(f"Fact probes failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
