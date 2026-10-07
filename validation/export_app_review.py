"""Export current model evaluations for independent, local human review.

The label template deliberately excludes JEV verdicts. Both files may contain
private prompts and answers, so the default destination is git-ignored data/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import evaluation as rubric
from server import AppHandler, connect, now_iso


def build_packet(db_path: Path, evaluator_model: str = "jev-1.13.0") -> tuple[dict, list[dict]]:
    handler = object.__new__(AppHandler)
    handler.db_path = db_path
    handler.evaluator_model = evaluator_model
    items = handler.list_results("model")["items"]
    with connect(db_path) as db:
        context = {row["id"]: row for row in db.execute(
            "SELECT id, attachment_text, conversation_history FROM cases WHERE evaluation_mode = 'model'")}
    runs, labels = [], []
    for item in items:
        report = item.get("report")
        if item["stale"] or not report or not report.get("factVerification"):
            continue
        fingerprint = report.get("evaluationFingerprint")
        if not fingerprint:
            continue
        review_id = f"response-{item['responseId']}-evaluation-{item['evaluationId']}"
        runs.append({"id": review_id, "split": "holdout", "origin": "app_review_sample",
                     "caseHash": fingerprint, "category": item["category"], "report": report})
        case_context = context[item["caseId"]]
        spec = item["evaluationSpec"]
        claims = (report["factVerification"] or {}).get("claims", [])
        visible = ((report.get("inspection") or {}).get("observations") or {}).get("visibleText")
        labels.append({"id": review_id, "caseHash": fingerprint, "reviewer": "",
                       "humanReviewed": False, "evaluationId": item["evaluationId"],
                       "caseId": item["caseId"], "responseId": item["responseId"],
                       "title": item["title"], "category": item["category"],
                       "model": item["model"], "prompt": item["prompt"],
                       "conversationHistory": case_context["conversation_history"],
                       "answer": item["content"], "visibleAnswer": visible,
                       "attachment": case_context["attachment_text"],
                       "expectedAnswer": spec["expectedAnswer"],
                       "references": spec["references"],
                       "axes": {axis: None for axis in rubric.LABELS},
                       "claims": {claim["id"]: {"text": claim["text"],
                                                  "relation": None, "importance": None}
                                  for claim in claims},
                       "missingClaims": []})
    run_packet = {"rubricVersion": rubric.VERSION, "createdAt": now_iso(),
                  "fixtureProvenance": "Existing app evaluations; review sample is not an independent holdout",
                  "runs": runs}
    return run_packet, labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/evaluations.db")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/review")
    parser.add_argument("--model", default="jev-1.13.0")
    args = parser.parse_args()
    packet, labels = build_packet(args.db, args.model)
    if not labels:
        print("현재 기준으로 평가된 일반 모델 Truthfulness 결과가 없습니다. 재평가 후 다시 실행하세요.")
        return
    args.output_dir.mkdir(parents=True, exist_ok=True)
    paths = [args.output_dir / "app-review-run.json",
             args.output_dir / "app-review-labels.template.json"]
    if any(path.exists() for path in paths):
        parser.error("검토 파일이 이미 있습니다. 기존 파일을 보존하고 다른 --output-dir을 지정하세요.")
    for path, value in zip(paths, (packet, labels)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"검토할 결과 {len(labels)}건을 {args.output_dir}에 저장했습니다.")


if __name__ == "__main__":
    main()
