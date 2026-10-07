import unittest

import fact_verification as fact
import link_verification


class FactVerificationTests(unittest.TestCase):
    def test_fenced_code_does_not_consume_factual_claim_budget(self):
        answer='```python\n' + '\n'.join(f'print({i})' for i in range(30)) + \
            '\n```\n서비스는 무료 요금제에서 파일 내보내기를 지원합니다.'
        claims, skipped=fact.candidates(answer)
        self.assertEqual(list(claims.values()),
                         ['서비스는 무료 요금제에서 파일 내보내기를 지원합니다.'])
        self.assertEqual(skipped,0)

    def test_short_factual_answer_is_a_candidate(self):
        claims, skipped = fact.candidates("H2O입니다.")
        self.assertEqual((claims, skipped), ({"C1": "H2O입니다."}, 0))
        self.assertEqual(fact.candidates("4"), ({"C1": "4"}, 0))

    def test_repeated_identical_claim_counts_once(self):
        claims, skipped = fact.candidates("회의는 화요일입니다. 회의는  화요일입니다.\n회의는 화요일입니다.")
        self.assertEqual((claims, skipped), ({"C1": "회의는 화요일입니다."}, 0))
        rows = [{"id": "C1", "text": claims["C1"], "importance": "HIGH",
                 "relation": "CONTRADICTED", "sourceRef": "E1"}]
        self.assertEqual(fact.score(rows, 0)["score"], 1)

    def test_long_answer_samples_beginning_middle_and_end(self):
        answer="\n".join(f"주장 {i}입니다." for i in range(1, 41))
        claims, skipped=fact.candidates(answer)
        self.assertEqual((len(claims), skipped), (fact.MAX_CLAIMS, 20))
        self.assertEqual(claims['C1'], '주장 1입니다.')
        self.assertEqual(claims['C20'], '주장 40입니다.')
        self.assertTrue(any(text in claims.values() for text in
                            ('주장 19입니다.', '주장 20입니다.', '주장 21입니다.', '주장 22입니다.')))

    def test_unchecked_candidates_prevent_not_applicable_verdict(self):
        rows=[{"id": "C1", "text": "다음에는 토론을 권장합니다.", "importance": "NONE",
               "relation": "NOT_APPLICABLE", "sourceRef": "NONE"}]
        report=fact.score(rows, 8)
        self.assertIsNone(report['score'])
        self.assertEqual(report['status'], 'unverifiable')
        self.assertTrue(report['needsReview'])

    def test_uncertain_nonfact_labels_do_not_become_clear_not_applicable(self):
        claims={"C1":"행사는 화요일에 열립니다."}
        raw={"answers":{"fact_kind_C1":{"choice":"NONE","confidence":.38},
                         "fact_relation_C1":{"choice":"INSUFFICIENT","confidence":.9},
                         "fact_source_C1":{"choice":"NONE","confidence":.9}}}
        uncertain=fact.score(fact.parse(raw,claims,{}),0)
        self.assertIsNone(uncertain['score'])
        self.assertEqual(uncertain['status'],'unverifiable')
        self.assertEqual(uncertain['lowConfidenceExcludedCount'],1)
        self.assertTrue(uncertain['needsReview'])
        raw['answers']['fact_kind_C1']['confidence']=.91
        clear=fact.score(fact.parse(raw,claims,{}),0)
        self.assertEqual(clear['status'],'not_applicable')
        self.assertFalse(clear['needsReview'])

    def test_exact_numeric_reference_requires_single_registered_number(self):
        rows = [{"id": "C1", "text": "4.0", "importance": "HIGH",
                 "relation": "UNVERIFIED", "sourceRef": "NONE", "sourceText": None}]
        fact.apply_exact_numeric_reference(rows, "4", {"E1": "4"},
                                           {"E1": {"kind": "reference_answer"}})
        self.assertEqual(rows[0]["relation"], "SUPPORTED")
        self.assertEqual(rows[0]["verificationMethod"], "exact_numeric_reference")
        other = [{"id": "C1", "text": "5", "importance": "HIGH",
                  "relation": "UNVERIFIED", "sourceRef": "NONE", "sourceText": None}]
        fact.apply_exact_numeric_reference(other, "2+2의 답은 4입니다.", {"E1": "2+2의 답은 4입니다."},
                                           {"E1": {"kind": "reference_answer"}})
        self.assertEqual(other[0]["relation"], "UNVERIFIED")

    def test_markdown_table_values_remain_claim_candidates(self):
        claims, skipped = fact.candidates("| 항목 | 지원 여부 |\n|---|---|\n| 무료 요금제 | 파일 내보내기 불가 |")
        self.assertEqual(skipped, 0)
        self.assertTrue(any("파일 내보내기 불가" in text for text in claims.values()))

    def test_cited_page_excerpt_finds_relevant_late_section(self):
        visible = ("서론과 목차 내용입니다. " * 500) + "무료 요금제에서는 파일 내보내기를 지원하지 않습니다."
        excerpt = link_verification._focused_excerpt(visible, "무료 요금제에서는 파일 내보내기를 지원합니다.")
        self.assertIn("파일 내보내기를 지원하지 않습니다", excerpt)

    def test_supported_core_claim_is_scored(self):
        claims, skipped = fact.candidates("이 제품은 무료 요금제에서 파일을 내보낼 수 있습니다.")
        self.assertEqual(skipped, 0)
        sources = {"E1": "무료 요금제는 파일 내보내기를 지원합니다."}
        raw = {"answers": {"fact_kind_C1": {"choice": "HIGH"},
                           "fact_relation_C1": {"choice": "SUPPORTED"},
                           "fact_source_C1": {"choice": "E1"}}}
        rows = fact.parse(raw, claims, sources)
        self.assertEqual(fact.score(rows, 0)["score"], 3)

    def test_low_confidence_verdict_keeps_score_but_requires_review(self):
        claims={"C1":"무료 요금제는 파일 내보내기를 지원합니다."}
        raw={"answers":{"fact_kind_C1":{"choice":"HIGH","confidence":.92},
                         "fact_relation_C1":{"choice":"CONTRADICTED","confidence":.41},
                         "fact_source_C1":{"choice":"E1","confidence":.96}}}
        rows=fact.parse(raw,claims,{"E1":"무료 요금제는 파일 내보내기를 지원하지 않습니다."})
        result=fact.score(rows,0)
        self.assertEqual(result['score'],1)
        self.assertEqual(result['lowConfidenceVerifiedCount'],1)
        self.assertEqual(rows[0]['lowConfidenceFields'],['relationConfidence'])
        self.assertTrue(result['needsReview'])
        raw['answers']['fact_relation_C1']['confidence']=.91
        stable=fact.score(fact.parse(raw,claims,{"E1":"무료 요금제는 파일 내보내기를 지원하지 않습니다."}), 0)
        self.assertEqual(stable['lowConfidenceVerifiedCount'],0)
        self.assertFalse(stable['needsReview'])

    def test_missing_confidence_is_reviewed_not_silently_trusted(self):
        claims={"C1":"출시일은 화요일입니다."}
        raw={"answers":{"fact_kind_C1":{"choice":"HIGH"},
                         "fact_relation_C1":{"choice":"SUPPORTED"},
                         "fact_source_C1":{"choice":"E1"}}}
        result=fact.score(fact.parse(raw,claims,{"E1":"출시일은 화요일입니다."}), 0)
        self.assertEqual(result['score'],3)
        self.assertEqual(result['lowConfidenceVerifiedCount'],1)
        self.assertTrue(result['needsReview'])

    def test_unknown_claim_has_no_score_and_requires_review(self):
        claims = {"C1": "다음 달에 새 기능이 출시됩니다."}
        raw = {"answers": {"fact_kind_C1": {"choice": "HIGH"},
                           "fact_relation_C1": {"choice": "INSUFFICIENT"},
                           "fact_source_C1": {"choice": "NONE"}}}
        verdict = fact.score(fact.parse(raw, claims, {}), 0)
        self.assertIsNone(verdict["score"])
        self.assertEqual(verdict["status"], "unverifiable")
        self.assertTrue(verdict["needsReview"])
        self.assertEqual(verdict["verifiedCount"], 0)

    def test_without_sources_asks_only_claim_kind(self):
        claims = {"C1": "다음 달에 새 기능이 출시됩니다."}
        self.assertEqual(list(fact.questions(claims, {})), ["fact_kind_C1"])
        rows = fact.parse({"answers": {"fact_kind_C1": {"choice": "HIGH", "confidence": .95}}}, claims, {})
        self.assertEqual(rows[0]["relation"], "UNVERIFIED")
        self.assertIsNone(fact.score(rows, 0)["score"])

    def test_partial_evidence_scores_confirmed_contradiction_and_flags_unknown(self):
        rows = [
            {"id": "C1", "text": "무료 요금제는 파일을 내보낼 수 있습니다.",
             "importance": "HIGH", "relation": "CONTRADICTED", "sourceRef": "E1",
             "importanceConfidence": .95, "relationConfidence": .95, "sourceConfidence": .95},
            {"id": "C2", "text": "다음 달에 기능이 출시됩니다.",
             "importance": "HIGH", "relation": "UNVERIFIED", "sourceRef": "NONE"},
        ]
        result = fact.score(rows, 0)
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["verifiedCount"], 1)
        self.assertEqual(result["unverifiedCount"], 1)
        self.assertTrue(result["needsReview"])


if __name__ == "__main__":
    unittest.main()
