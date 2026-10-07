"""Compare repeated JEV verdicts against a pinned local app review packet."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import evaluation as rubric
from server import AppHandler, load_env_file, now_iso


def signature(report: dict) -> dict:
    fact = report.get("factVerification") or {}
    return {"score": report["axes"]["truthfulness"]["score"],
            "status": report["axes"]["truthfulness"]["status"],
            "claims": {row["id"]: row["relation"] for row in fact.get("claims", [])},
            "unverifiedCount": fact.get("unverifiedCount"),
            "lowConfidenceVerifiedCount": fact.get("lowConfidenceVerifiedCount", 0),
            "needsReview": report["axes"]["truthfulness"].get("needsReview", False),
            "skippedCandidateCount": fact.get("skippedCandidateCount")}


def summarize(rows: list[dict]) -> dict:
    complete = [row for row in rows if len(row["runs"]) >= 3 and not row["errors"]]
    scores_changed = [row["responseId"] for row in complete
                      if len({json.dumps(run["score"]) for run in row["runs"]}) > 1]
    relations_changed = [row["responseId"] for row in complete
                         if len({json.dumps(run["claims"], sort_keys=True, ensure_ascii=False)
                                 for run in row["runs"]}) > 1]
    low_confidence_flagged = [row["responseId"] for row in complete
                              if any(run.get("lowConfidenceVerifiedCount", 0) for run in row["runs"])]
    return {"completeResponses": len(complete), "scoreChangedResponses": scores_changed,
            "claimRelationChangedResponses": relations_changed,
            "lowConfidenceFlaggedResponses": low_confidence_flagged,
            "scoreChangedWithLowConfidenceFlag": sorted(set(scores_changed) & set(low_confidence_flagged)),
            "scoreStability": 1 - len(scores_changed) / len(complete) if complete else None,
            "relationStability": 1 - len(relations_changed) / len(complete) if complete else None,
            "failedResponses": [row["responseId"] for row in rows if row["errors"]]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/evaluations.db")
    parser.add_argument("--packet", type=Path, default=ROOT / "data/review/app-review-run.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/review/stability.json")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--limit", type=int, default=0, help="0이면 검토 묶음 전체")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit은 0 이상이어야 합니다.")
    if args.output.exists():
        parser.error("안정성 결과가 이미 있습니다. 다른 --output을 지정해 기존 결과를 보존하세요.")
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    if packet.get("rubricVersion") != rubric.VERSION:
        parser.error("검토 묶음과 현재 평가 기준 버전이 다릅니다.")
    load_env_file()
    handler = object.__new__(AppHandler)
    handler.db_path = args.db
    handler.evaluator_model = args.model
    selected = packet["runs"][:args.limit or None]
    rows = []
    for index, run in enumerate(selected, 1):
        response_id = int(run["id"].split("-")[1])
        row = {"responseId": response_id, "category": run["category"],
               "runs": [signature(run["report"])], "errors": []}
        for repeat in (2, 3):
            try:
                report = handler.evaluate(response_id, force=True)["report"]
                if report.get("evaluationFingerprint") != run["caseHash"]:
                    raise RuntimeError("기준 검토 묶음과 평가 입력이 달라졌습니다.")
                row["runs"].append(signature(report))
            except (RuntimeError, ValueError) as error:
                row["errors"].append({"repeat": repeat, "message": str(error)[:160]})
                break
        rows.append(row)
        print(f"{index}/{len(selected)} 응답 {response_id}: "
              f"반복 {len(row['runs'])}회, 오류 {len(row['errors'])}건", flush=True)
    output = {"rubricVersion": rubric.VERSION, "createdAt": now_iso(),
              "sourcePacket": str(args.packet), "responses": rows, "summary": summarize(rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
