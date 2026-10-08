"""Reproducible synthetic claim-limit experiment; leaves production code and DB untouched.

python validation/claim_limit_experiment.py --output data/claim-limit --offline
python validation/claim_limit_experiment.py --output data/claim-limit --live
python validation/claim_limit_experiment.py --output data/claim-limit --report

JEV expectations are authored synthetic labels, not independent human validation.
Completed jobs are appended to JSONL and skipped on resume, including failures.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import evaluation as rubric
import fact_verification as fact
from rendering import inspect_html
from server import call_jev, load_env_file

CAPS = (10, 20, 40, "all")
VARIANTS = ("normal", "first", "middle", "last", "multiple", "paraphrase")
LIVE_CASES = (
    "10-text-normal", "10-text-first", "20-markdown-last", "20-text-middle",
    "20-html-paraphrase", "40-text-first", "40-text-middle", "40-text-last",
    "40-text-multiple", "40-markdown-normal", "40-html-middle", "40-html-paraphrase",
    "80-text-first", "80-text-last", "80-text-multiple", "80-html-normal",
)
REPEAT_CASES = {"40-text-middle", "40-text-last", "40-html-middle", "80-text-last"}


def statement(index: int, kind: int, wrong: bool = False, paraphrase: bool = False) -> str:
    subject = f"오르빛의 항목 {index:02d}"
    if kind == 0:
        value = 10 + index + (7 if wrong else 0)
        return f"{subject}의 좌석 수는 {value}개입니다." if not paraphrase else f"{subject}에 등록된 좌석은 총 {value}개입니다."
    if kind == 1:
        day = (index % 27) + 1 + int(wrong)
        return f"{subject}의 점검일은 2026년 11월 {day}일입니다." if not paraphrase else f"{subject}는 2026년 11월 {day}일에 점검합니다."
    if kind == 2:
        verb = "지원하지 않습니다" if wrong else "지원합니다"
        return f"{subject}은 이메일 알림을 {verb}." if not paraphrase else f"{subject}에서 이메일로 알림을 보내는 기능을 {verb}."
    person = f"{'나래' if wrong else '가람'}{index:02d}"
    return f"{subject}의 발표 담당자는 {person}입니다." if not paraphrase else f"{subject}의 발표는 {person}이 맡습니다."


def make_fixtures() -> list[dict]:
    fixtures = []
    for count in (10, 20, 40, 80):
        for fmt in ("text", "markdown", "html"):
            for variant in VARIANTS:
                errors = ({1: 0} if variant == "first" else
                          {count // 2: 3 if fmt == "html" else 1} if variant == "middle" else
                          {count: 2} if variant == "last" else
                          {1: 0, count // 2: 1, count: 3} if variant == "multiple" else {})
                source, gold, lines = [], [], []
                for i in range(1, count + 1):
                    kind = errors.get(i, (i - 1) % 4)
                    original = statement(i, kind)
                    response = statement(i, kind, i in errors, variant == "paraphrase")
                    relation = "CONTRADICTED" if i in errors else "SUPPORTED"
                    if variant == "paraphrase" and i % 10 == 0:
                        response = f"오르빛의 항목 {i:02d}은 다음 점검 전에 다시 확인하는 것이 좋겠습니다."
                        relation = "NOT_APPLICABLE"
                    source.append(original)
                    lines.append(response)
                    gold.append({"id": f"F{i:03d}", "text": response, "relation": relation,
                                 "errorType": ("number", "date", "support", "attribution")[kind] if i in errors else None})
                sections = [f"S{start // 10 + 1}. 항목 {start + 1}~{min(start + 10, count)}\n" + "\n".join(source[start:start + 10])
                            for start in range(0, count, 10)]
                answer = ("\n".join(lines) if fmt == "text" else
                          "| |\n|---|\n" + "\n".join(f"| {line} |" for line in lines) if fmt == "markdown" else
                          "<!doctype html><html lang='ko'><head><meta charset='utf-8'></head><body>" +
                          "".join(f"<p>{html.escape(line)}</p>" for line in lines) +
                          "<p style='display:none'>숨긴 시험 문장은 실제 화면의 주장에 포함하지 않습니다.</p></body></html>")
                case_id = f"{count}-{fmt}-{variant}"
                fixtures.append({"id": case_id, "length": count, "format": fmt, "variant": variant,
                                 "prompt": "등록한 문서에 따라 오르빛의 각 항목에 관한 좌석 수, 점검일, 지원 여부와 담당자를 정리해 주세요.",
                                 "attachment": "\n".join(sections), "answer": answer, "gold": gold,
                                 "live": case_id in LIVE_CASES, "repeats": 3 if case_id in REPEAT_CASES else 1})
    return fixtures


def normalize(text: str) -> str:
    return " ".join(text.strip().strip("|").strip().split())


def selected_claims(visible: str, cap: int | str) -> tuple[dict, int]:
    limit = max(len(visible.splitlines()) + 10, 2) if cap == "all" else int(cap)
    with patch.object(fact, "MAX_CLAIMS", limit):
        return fact.candidates(visible)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def infrastructure_failure(job: dict) -> bool:
    return not job.get("success") and "10013" in job.get("error", "")


def latest_jobs(jobs: list[dict]) -> list[dict]:
    latest = {}
    for job in jobs:
        latest[(job["case"], str(job["cap"]), job["repeat"])] = job
    return list(latest.values())


def prepare(output: Path) -> tuple[list[dict], list[dict]]:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "fixtures.json"
    fixtures = make_fixtures()
    identity = hashlib.sha256(json.dumps(fixtures, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if path.exists():
        packet = json.loads(path.read_text(encoding="utf-8"))
        if packet["fixtureHash"] != identity:
            raise RuntimeError("Fixture definitions changed; use a new output directory.")
        if packet["rubricVersion"] != rubric.VERSION:
            raise RuntimeError("Production rubric version changed; use a new output directory.")
        fixtures = packet["cases"]
    else:
        for case in fixtures:
            if case["format"] == "html":
                observed = inspect_html(case["answer"], output / "artifacts")
                if not observed.get("rendered"):
                    raise RuntimeError(f"HTML observation failed: {observed.get('reason')}")
                case["visibleText"] = observed["observations"]["visibleText"]
            else:
                case["visibleText"] = case["answer"]
        write_json(path, {"fixtureHash": identity, "provenance": "AI-authored synthetic fixtures, no independent human labels",
                          "rubricVersion": rubric.VERSION, "cases": fixtures})
    offline = []
    for case in fixtures:
        gold = {normalize(item["text"]): item for item in case["gold"]}
        for cap in CAPS:
            claims, skipped = selected_claims(case["visibleText"], cap)
            selected = {gold[normalize(text)]["id"] for text in claims.values() if normalize(text) in gold}
            errors = {item["id"] for item in case["gold"] if item["relation"] == "CONTRADICTED"}
            offline.append({"case": case["id"], "cap": cap, "candidateCount": len(claims), "skipped": skipped,
                            "selectedIds": sorted(selected), "errorIds": sorted(errors),
                            "selectedErrorIds": sorted(selected & errors), "omittedErrorIds": sorted(errors - selected),
                            "unexpectedCandidates": [text for text in claims.values() if normalize(text) not in gold],
                            "missingGoldIds": sorted(set(item["id"] for item in case["gold"]) - selected)})
    write_json(output / "offline.json", offline)
    return fixtures, offline


def run_job(case: dict, cap: int | str, repeat: int, model: str, batch_size: int) -> dict:
    claims, skipped = selected_claims(case["visibleText"], cap)
    sources = rubric.segments(case["attachment"], "E", section_headings=True)
    state = {"user_prompt": case["prompt"], "claims": claims, "sources": sources,
             "verification_scope": "등록 자료와의 비교. 자료의 진위는 검증하지 않음."}
    mapping = {normalize(item["text"]): item for item in case["gold"]}
    gold_ids = {cid: mapping[normalize(text)]["id"] for cid, text in claims.items()}
    result = {"case": case["id"], "cap": cap, "repeat": repeat, "model": model,
              "batchSize": batch_size, "rubricVersion": rubric.VERSION, "candidateCount": len(claims),
              "skipped": skipped, "selectedGoldIds": list(gold_ids.values()), "calls": [], "rows": []}
    began = time.perf_counter()
    chunks = [dict(list(claims.items())[i:i + (batch_size or len(claims))])
              for i in range(0, len(claims), batch_size or len(claims))]
    answers = {}
    try:
        for chunk in chunks:
            request_state = {**state, "claims": chunk}
            questions = fact.questions(chunk, sources)
            call = {"questionCount": len(questions), "payloadBytes": len(json.dumps(
                {"model": model, "state": request_state, "questions": questions}, ensure_ascii=False).encode())}
            result["calls"].append(call)
            call_began = time.perf_counter()
            try:
                raw = call_jev(request_state, model, questions)
            finally:
                call["elapsedSeconds"] = time.perf_counter() - call_began
            call["usage"] = raw.get("usage", {})
            call["returnedModel"] = raw.get("model")
            answers.update(raw.get("answers", {}))
        rows = fact.parse({"answers": answers}, claims, sources)
        report = fact.score(rows, skipped)
        for row in rows:
            row["goldId"] = gold_ids[row["id"]]
            row["expectedRelation"] = mapping[normalize(row["text"])]["relation"]
        result.update({"rows": rows, "report": report, "success": True})
    except Exception as exc:
        result.update({"success": False, "error": str(exc)[:700]})
    result["elapsedSeconds"] = time.perf_counter() - began
    return result


def summarize(fixtures: list[dict], offline: list[dict], jobs: list[dict]) -> dict:
    by_case = {case["id"]: case for case in fixtures}
    summaries = []
    for cap in CAPS:
        selection = [row for row in offline if row["cap"] == cap]
        runs = [job for job in jobs if job["cap"] == cap]
        # First repeats only for accuracy; repeat jobs are stability evidence.
        primary = [job for job in runs if job["repeat"] == 1]
        successful = [job for job in primary if job["success"]]
        error_count = sum(len(row["errorIds"]) for row in selection)
        selected_errors = sum(len(row["selectedErrorIds"]) for row in selection)
        selected_live_errors = caught = false_contradictions = judged = missed_in_selection = clean = flagged = abstained = 0
        error_cases = normal_cases = normal_review = correct_links = link_count = 0
        wrong_links = []
        normal_review_details = []
        for job in successful:
            gold = by_case[job["case"]]["gold"]
            errors = {row["id"] for row in gold if row["relation"] == "CONTRADICTED"}
            error_cases += bool(errors)
            normal_cases += not errors
            normal_review += not errors and job["report"]["needsReview"]
            if not errors and job["report"]["needsReview"]:
                normal_review_details.append({"case": job["case"], "skipped": job["skipped"],
                    "claims": [{"goldId": row["goldId"], "fields": row["factVerdictReviewFields"]}
                               for row in job["rows"] if row.get("factVerdictReviewFields")]})
            included = errors & set(job["selectedGoldIds"])
            selected_live_errors += len(included)
            missed_in_selection += len(errors - included)
            caught += sum(row["goldId"] in errors and row["relation"] == "CONTRADICTED" for row in job["rows"])
            false_contradictions += sum(row["goldId"] not in errors and row["relation"] == "CONTRADICTED" for row in job["rows"])
            judged += sum(row["expectedRelation"] == row["relation"] for row in job["rows"])
            for row in job["rows"]:
                if row["expectedRelation"] in {"SUPPORTED", "CONTRADICTED"}:
                    link_count += 1
                    expected_source = f"E{(int(row['goldId'][1:]) - 1) // 10 + 1}"
                    correct_links += row["sourceRef"] == expected_source
                    if row["sourceRef"] != expected_source:
                        wrong_links.append({"case": job["case"], "goldId": row["goldId"],
                            "expectedSource": expected_source, "selectedSource": row["sourceRef"],
                            "confidence": row["sourceConfidence"],
                            "flagged": bool(row["factVerdictReviewFields"])})
            if errors:
                report = job["report"]
                if report["score"] is None: abstained += 1
                elif report["score"] == 3:
                    if report["needsReview"]: flagged += 1
                    else: clean += 1
        elapsed = sorted(job["elapsedSeconds"] for job in successful)
        usage = {}
        primary_usage = {}
        for job in runs:
            for call in job["calls"]:
                for key, value in call.get("usage", {}).items():
                    if type(value) in (int, float):
                        usage[key] = usage.get(key, 0) + value
                        if job["repeat"] == 1:
                            primary_usage[key] = primary_usage.get(key, 0) + value
        unstable = []
        for cid in REPEAT_CASES:
            repeated = [job for job in runs if job["case"] == cid and job["success"]]
            signatures = {(job["report"]["score"], tuple((r["goldId"], r["relation"]) for r in job["rows"])) for job in repeated}
            if len(signatures) > 1: unstable.append(cid)
        summaries.append({"cap": cap, "offlineErrorCount": error_count, "offlineSelectedErrors": selected_errors,
            "offlineErrorCoverage": selected_errors / error_count if error_count else None,
            "liveJobs": len(runs), "failedJobs": sum(not job["success"] for job in runs),
            "primarySuccesses": len(successful), "primaryCases": len(primary),
            "selectionMissedLiveErrors": missed_in_selection, "selectedLiveErrors": selected_live_errors,
            "caughtLiveErrors": caught, "judgeMissedSelectedErrors": selected_live_errors - caught,
            "goldLiveErrors": selected_live_errors + missed_in_selection,
            "endToEndErrorRecall": caught / (selected_live_errors + missed_in_selection) if selected_live_errors + missed_in_selection else None,
            "falseContradictions": false_contradictions,
            "relationAgreement": judged / sum(len(job["rows"]) for job in successful) if successful else None,
            "sourceLinkAgreement": correct_links / link_count if link_count else None,
            "wrongSourceLinks": wrong_links, "normalReviewDetails": normal_review_details,
            "falseClearPasses": clean, "threePointButFlagged": flagged, "abstainedErrorCases": abstained,
            "goldErrorCases": error_cases, "normalCases": normal_cases, "normalReviewCases": normal_review,
            "medianSeconds": statistics.median(elapsed) if elapsed else None,
            "p95Seconds": elapsed[math.ceil(.95 * len(elapsed)) - 1] if elapsed else None,
            "maxSeconds": max(elapsed) if elapsed else None,
            "usage": usage, "primaryUsage": primary_usage, "unstableRepeatCases": unstable})
    return {"rubricVersion": rubric.VERSION, "caps": summaries, "fixtureCases": len(fixtures),
            "configuredLiveCases": list(LIVE_CASES), "repeatCases": sorted(REPEAT_CASES),
            "jobs": len(jobs), "limitations": ["Synthetic expected labels are not independent human labels.",
                "Only Truthfulness questions are timed; other app axes and network evidence collection are excluded.",
                "The fixtures are one fictional domain with explicitly registered sources.",
                "No monetary cost is inferred without a verified API pricing contract.",
                "Requests run sequentially in fixed cap order; timing is not a randomized service benchmark.",
                "The many independent item facts are easier than multi-clause interpretations in real reports."]}


def report(output: Path, fixtures: list[dict], offline: list[dict]) -> None:
    attempts = [json.loads(line) for line in (output / "live.jsonl").read_text(encoding="utf-8").splitlines()] if (output / "live.jsonl").exists() else []
    jobs = latest_jobs(attempts)
    summary = summarize(fixtures, offline, jobs)
    summary["infrastructureFailureAttempts"] = sum(infrastructure_failure(job) for job in attempts)
    expected_keys = {(cid, str(cap), repeat) for cid in LIVE_CASES for cap in CAPS
                     for repeat in range(1, 4 if cid in REPEAT_CASES else 2)}
    observed_keys = {(job['case'], str(job['cap']), job['repeat']) for job in jobs}
    summary["completionAudit"] = {
        "expectedJobs": len(expected_keys), "missingJobs": sorted(expected_keys - observed_keys),
        "failedJobs": sum(not job["success"] for job in jobs),
        "unexpectedCandidates": sum(len(row["unexpectedCandidates"]) for row in offline),
        "fullSelectionMissingGold": sum(len(row["missingGoldIds"]) for row in offline if row["cap"] == "all"),
        "returnedModels": sorted({call.get("returnedModel", "") for job in jobs for call in job["calls"] if call.get("returnedModel")}),
        "callsMissingUsage": sum(not call.get("usage") for job in jobs if job["success"] for call in job["calls"]),
    }
    write_json(output / "summary.json", summary)
    headers = ["상한", "합성 오류 후보 선택률", "실호출 성공/첫 실행", "실평가 오류 탐지", "선택됐으나 놓친 오류", "잘못된 모순", "3점·검토 필요", "중앙 시간(초)", "전체 실패/실행", "첫 실행 사용량"]
    rows = []
    for row in summary["caps"]:
        cells = [row["cap"], f"{row['offlineSelectedErrors']}/{row['offlineErrorCount']} ({row['offlineErrorCoverage']:.1%})",
                 f"{row['primarySuccesses']}/{row['primaryCases']}", f"{row['caughtLiveErrors']}/{row['goldLiveErrors']}",
                 row["judgeMissedSelectedErrors"], row["falseContradictions"], row["threePointButFlagged"],
                 f"{row['medianSeconds']:.2f}" if row["medianSeconds"] is not None else "—",
                 f"{row['failedJobs']}/{row['liveJobs']}", json.dumps(row["primaryUsage"], ensure_ascii=False)]
        rows.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>")
    errors = [job for job in jobs if not job["success"]]
    document = """<!doctype html><html lang='ko'><meta charset='utf-8'><title>후보 상한 실험</title>
<style>body{font:15px/1.6 sans-serif;max-width:1400px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:8px;text-align:left}pre{white-space:pre-wrap;background:#eee;padding:12px}small{color:#555}</style>
<h1>Truthfulness 후보 상한 합성 실험</h1>"""
    document += f"<p>예제 {len(fixtures)}개, 실제 평가 실행 {len(jobs)}건. 기대 판정은 작성된 합성 정답이며 독립적인 사람 검증이 아닙니다.</p>"
    document += "<table><tr>" + "".join(f"<th>{heading}</th>" for heading in headers) + "</tr>" + "".join(rows) + "</table>"
    document += "<h2>실험 조건</h2><p>문장 후보 10·20·40·80개, 일반 텍스트·Markdown 표·브라우저에서 관측한 HTML 본문. 정상·단일 오류(앞/중간/끝)·복수 오류·바꿔쓰기/권고. 후보 선택은 현재 앱의 균등 선택 함수를 그대로 사용합니다. C번호 대신 원래 예제의 F번호로 판정을 비교합니다.</p>"
    document += "<p>상한별 첫 실행만 탐지 정확도에 사용하고 경계 사례의 3회 실행은 안정성에 별도 사용합니다. 선택 누락은 Jev 판단 오류에 포함하지 않습니다. 전체 검사에서도 후보 추출 방식 자체의 누락 가능성은 남습니다.</p>"
    document += "<p>처음 로컬 소켓 권한으로 막힌 1회는 기록을 보존하고 네트워크 권한 승인 후 재실행했습니다. 최종 성공/실패 집계는 각 조건의 마지막 실행을 사용하며, 환경 차단은 모델 판단 실패로 계산하지 않습니다.</p>" if summary["infrastructureFailureAttempts"] else ""
    if expected_keys <= observed_keys and all(job["success"] for job in jobs):
        cap20 = next(row for row in summary['caps'] if row['cap'] == 20)
        full = next(row for row in summary['caps'] if row['cap'] == 'all')
        document += f"<h2>관찰 결과와 권고</h2><p><strong>이 합성 실험에서는 검사 범위 확대가 유리했습니다.</strong> 20개 제한은 실제 평가용 예제의 오류 {cap20['goldLiveErrors']}개 중 {cap20['selectedLiveErrors']}개를 검사했고, {cap20['caughtLiveErrors']}개를 발견했습니다. 전체 검사는 {full['goldLiveErrors']}개 중 {full['caughtLiveErrors']}개를 발견했습니다. 균등 표본 20개를 긴 리포트의 최종 점수 근거로 삼는 방식은 재검토하는 것을 권고합니다.</p>"
        document += f"<p>20개 조건의 오류 답변 {cap20['threePointButFlagged']}개는 모순을 찾지 못해 3점이면서 검토 필요로 남았습니다. 3점·검토 없음으로 통과한 오류 답변은 {cap20['falseClearPasses']}개였습니다. 상한 밖 후보가 있으면 자동으로 검토 표시가 붙는 구현의 영향이므로 검토 표시만으로 오류 탐지 성공이라고 간주하면 안 됩니다.</p>"
        document += "<p>전체 검사는 이번 80문장 이하·최대 8개 근거 절에서 단일 요청으로 성공했습니다. 이 규모에서는 제한을 늘렸을 때 속도보다 토큰 사용량 증가가 주요 비용이었습니다. 다만 한 가지 가상 영역의 명확한 사실만 사용했고 요청 순서도 고정돼 있으므로 80개 또는 무제한이 업무 전반의 최적 상한이라고 확정할 수 없습니다.</p>"
        document += "<p><strong>다음 검증:</strong> 실제 긴 답변에서 전체 검사와 20개 균등 선택을 비교하고, 사람이 중요 오류를 먼저 표시해야 합니다. 자료가 더 길거나 근거 절 수가 많아 요청 한도를 넘으면 전체 후보를 여러 요청으로 나누는 방식도 별도 비교해야 합니다. 숫자·날짜만 우선 선택하는 방식은 의미·귀속 오류를 놓칠 수 있어 현재 증거만으로 채택하지 않습니다.</p>"
    else:
        document += "<h2>실험 상태</h2><p>계획된 96개 실행이 모두 완료되기 전에는 권고안을 확정하지 않습니다. 현재 표는 중간 결과입니다.</p>"
    document += "<h2>반복 안정성과 검토 부담</h2><table><tr><th>상한</th><th>3점·검토 없음 오류 답변</th><th>정상 답변의 검토 표시</th><th>반복 판정 변동 사례</th><th>P95 시간(초)</th><th>근거 절 연결 일치율</th></tr>"
    for row in summary["caps"]:
        cells = [row["cap"], row["falseClearPasses"], f"{row['normalReviewCases']}/{row['normalCases']}",
                 ", ".join(row["unstableRepeatCases"]) or "없음",
                 f"{row['p95Seconds']:.3f}" if row["p95Seconds"] is not None else "—",
                 f"{row['sourceLinkAgreement']:.1%}" if row["sourceLinkAgreement"] is not None else "—"]
        document += "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>"
    document += "</table>"
    document += "<h2>근거 연결 오류의 구체적 확인</h2><p>관계 판정(일치/모순)이 정답과 같아도 선택한 근거 절이 틀릴 수 있습니다. 따라서 관계 판정 일치율을 근거까지 정확한 평가라고 해석할 수 없습니다. 전체 검사 권고에도 근거 연결 검증이 함께 필요합니다.</p>"
    for row in summary["caps"]:
        unflagged = sum(not item["flagged"] for item in row["wrongSourceLinks"])
        document += f"<p>상한 {row['cap']}: 잘못된 근거 연결 {len(row['wrongSourceLinks'])}건, 그중 검토 표시 없는 연결 {unflagged}건.</p>"
    document += "<details><summary>정상 답변의 검토 사유: 후보 누락과 근거 판정 불확실성</summary><pre>" + html.escape(json.dumps(
        [{"cap": row["cap"], **item} for row in summary["caps"] for item in row["normalReviewDetails"]], ensure_ascii=False, indent=2)) + "</pre></details>"
    document += "<details><summary>잘못된 근거 연결 원시 판정</summary><pre>" + html.escape(json.dumps(
        [{"cap": row["cap"], **item} for row in summary["caps"] for item in row["wrongSourceLinks"]], ensure_ascii=False, indent=2)) + "</pre></details>"
    document += "<h2>길이·형식별 후보 선택 누락</h2><table><tr><th>구분</th><th>10개</th><th>20개</th><th>40개</th><th>전체</th></tr>"
    for field, values in (("length", (10, 20, 40, 80)), ("format", ("text", "markdown", "html"))):
        for value in values:
            case_ids = {case["id"] for case in fixtures if case[field] == value}
            cells = [f"{field}: {value}"]
            for cap in CAPS:
                subset = [row for row in offline if row["cap"] == cap and row["case"] in case_ids]
                cells.append(f"{sum(len(row['omittedErrorIds']) for row in subset)}/{sum(len(row['errorIds']) for row in subset)}")
            document += "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in cells) + "</tr>"
    document += "</table><p>표는 누락한 오류/전체 오류이며 실제 Jev 호출이 아닌 후보 선택 단계 72개 예제의 결과입니다.</p>"
    document += "<h2>실행 방법</h2><pre>.\\.venv\\Scripts\\python.exe validation/claim_limit_experiment.py --output data/claim-limit --offline\n.\\.venv\\Scripts\\python.exe validation/claim_limit_experiment.py --output data/claim-limit --live\n.\\.venv\\Scripts\\python.exe validation/claim_limit_experiment.py --output data/claim-limit --report</pre>"
    document += "<p>live.jsonl의 완료된 실행은 재개 시 건너뜁니다. 실패도 자동 재실행하지 않습니다. 다른 모델·배치 크기·수정된 예제 비교에는 새 출력 폴더를 사용합니다. API 키는 .env의 TYPESAFE_API_KEY에서 읽으며 결과에 저장하지 않습니다.</p>"
    document += "<h2>실패 기록</h2><pre>" + html.escape(json.dumps([{key: job[key] for key in ("case", "cap", "repeat", "error")} for job in errors], ensure_ascii=False, indent=2)) + "</pre>"
    document += "<h2>검증 범위</h2><p>표의 시간과 사용량은 Truthfulness 질문 평가에 한정됩니다. 긴 실제 업무 문서, AI Bot의 다른 평가 축, 출처 수집 시간은 포함하지 않습니다. 사용량은 API 반환값 그대로 집계하며 금액으로 환산하지 않습니다. 원문에서 문장 후보를 추리는 단계의 누락은 별도 위험으로 남습니다. 예제의 기대 판정은 수정에 사용한 합성 정답이며 독립적인 홀드아웃 검증으로 해석하지 않습니다.</p></html>"
    packet = json.loads((output / "fixtures.json").read_text(encoding="utf-8"))
    document = document.removesuffix("</html>")
    document += "<h2>실행 감사와 식별 정보</h2><pre>" + html.escape(json.dumps({
        "rubricVersion": rubric.VERSION, "fixtureHash": packet["fixtureHash"],
        "audit": summary["completionAudit"],
        "totalUsageIncludingRepeats": {key: sum(row["usage"].get(key, 0) for row in summary["caps"])
                                      for key in {key for row in summary["caps"] for key in row["usage"]}},
    }, ensure_ascii=False, indent=2)) + "</pre></html>"
    (output / "report.html").write_text(document, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/claim-limit")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--case", action="append")
    parser.add_argument("--caps", default="10,20,40,all")
    parser.add_argument("--model", default="jev-1.13.0")
    parser.add_argument("--batch-size", type=int, default=0, help="0: one request per condition; positive: fixed-size claim batches")
    parser.add_argument("--retry-infrastructure", action="store_true", help="retry local WinError 10013 socket permission failures; retain original attempt")
    args = parser.parse_args()
    if args.batch_size < 0: parser.error("batch-size must be nonnegative")
    caps = ["all" if part == "all" else int(part) for part in args.caps.split(",")]
    if any(cap not in CAPS for cap in caps): parser.error("caps must be 10,20,40,all")
    fixtures, offline = prepare(args.output)
    if args.live:
        load_env_file()
        path = args.output / "live.jsonl"
        existing = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        if any(job["model"] != args.model or job["batchSize"] != args.batch_size or job["rubricVersion"] != rubric.VERSION for job in existing):
            raise RuntimeError("Run configuration changed; use a new output directory.")
        done = {(job["case"], str(job["cap"]), job["repeat"]) for job in latest_jobs(existing)
                if not (args.retry_infrastructure and infrastructure_failure(job))}
        selected = [case for case in fixtures if case["id"] in args.case] if args.case else [case for case in fixtures if case["live"]]
        if not selected: parser.error("no matching cases")
        for repeat in range(1, 4):
            for case in selected:
                if repeat > case["repeats"]: continue
                for cap in caps:
                    if (case["id"], str(cap), repeat) in done: continue
                    job = run_job(case, cap, repeat, args.model, args.batch_size)
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(job, ensure_ascii=False) + "\n")
                        stream.flush()
                    print(json.dumps({key: job.get(key) for key in ("case", "cap", "repeat", "success", "elapsedSeconds", "error")}, ensure_ascii=True), flush=True)
                    report(args.output, fixtures, offline)
    report(args.output, fixtures, offline)
    print(json.dumps({"fixtures": len(fixtures), "offlineConditions": len(offline), "output": str(args.output)}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
