"""Add claim fields to unreviewed label templates without changing human labels."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fact_verification
from rendering import inspect_html


def main() -> None:
    cases = {case["id"]: case for case in json.loads(
        (ROOT / "validation/cases.json").read_text(encoding="utf-8"))}
    path = ROOT / "validation/cases.human-labels.template.json"
    labels = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for label in labels:
        case = cases.get(label["id"])
        if (not case or case["category"] == "코딩" or
                label.get("humanReviewed") or label.get("reviewer")):
            continue
        claim_text = case["response"]
        if case.get("outputFormat") == "html":
            rendered = inspect_html(claim_text, ROOT / "data/artifacts")
            claim_text = (rendered.get("observations") or {}).get("visibleText") or claim_text
        claims, _ = fact_verification.candidates(claim_text)
        proposed = {cid: {"text": claim, "relation": None, "importance": None}
                    for cid, claim in claims.items()}
        if label.get("claims") != proposed:
            label["claims"] = proposed
            changed = True
        if "missingClaims" not in label:
            label["missingClaims"] = []
            changed = True
    if changed:
        path.write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Updated" if changed else "Already current")


if __name__ == "__main__":
    main()
