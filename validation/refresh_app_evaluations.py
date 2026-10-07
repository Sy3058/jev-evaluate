"""Reevaluate stale general-model responses in the local app database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import AppHandler, load_env_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/evaluations.db")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--limit", type=int, default=0, help="0이면 모든 재평가 대상")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit은 0 이상이어야 합니다.")
    load_env_file()
    handler = object.__new__(AppHandler)
    handler.db_path = args.db
    handler.evaluator_model = args.model
    pending = [item for item in handler.list_results("model")["items"]
               if item["stale"] or item["report"] is None]
    if args.limit:
        pending = pending[:args.limit]
    print(f"재평가 대상 {len(pending)}건", flush=True)
    failed = []
    for index, item in enumerate(pending, 1):
        try:
            result = handler.evaluate(item["responseId"])
            fact = result["report"].get("factVerification") or {}
            print(f"{index}/{len(pending)} 응답 {item['responseId']}: "
                  f"Truthfulness {result['report']['axes']['truthfulness']['score']}, "
                  f"미검증 {fact.get('unverifiedCount', 0)}", flush=True)
        except (RuntimeError, ValueError) as error:
            failed.append(item["responseId"])
            print(f"{index}/{len(pending)} 응답 {item['responseId']}: 오류 {str(error)[:160]}", flush=True)
    print(f"완료 {len(pending) - len(failed)}건, 실패 {len(failed)}건", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
