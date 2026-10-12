"""Check experiment accounting without making API requests."""
import unittest

from validation import claim_limit_experiment as experiment


class ClaimLimitExperimentTests(unittest.TestCase):
    def test_fixture_matrix_and_error_labels(self):
        fixtures = experiment.make_fixtures()
        self.assertEqual(len(fixtures), 72)
        self.assertEqual(len({case['id'] for case in fixtures}), 72)
        for case in fixtures:
            expected = 3 if case['variant'] == 'multiple' else int(case['variant'] in {'first', 'middle', 'last'})
            self.assertEqual(sum(row['relation'] == 'CONTRADICTED' for row in case['gold']), expected)
            self.assertEqual(len(case['gold']), case['length'])
        self.assertEqual(sum(4 * case['repeats'] for case in fixtures if case['live']), 96)

    def test_stable_ids_expose_selection_omission(self):
        original_limit = experiment.fact.MAX_CLAIMS
        case = next(case for case in experiment.make_fixtures() if case['id'] == '40-text-middle')
        mapping = {experiment.normalize(row['text']): row['id'] for row in case['gold']}
        selected, skipped = experiment.selected_claims(case['answer'], 20)
        selected_ids = {mapping[experiment.normalize(text)] for text in selected.values()}
        self.assertNotIn('F020', selected_ids)
        self.assertEqual(skipped, 20)
        full, skipped = experiment.selected_claims(case['answer'], 'all')
        self.assertIn('F020', {mapping[experiment.normalize(text)] for text in full.values()})
        self.assertEqual(skipped, 0)
        # Mock cap is scoped to the experiment and restored after each condition.
        self.assertEqual(experiment.fact.MAX_CLAIMS, original_limit)

    def test_resume_uses_latest_attempt(self):
        blocked = {'case': 'x', 'cap': 20, 'repeat': 1, 'success': False, 'error': 'WinError 10013'}
        passed = {**blocked, 'success': True}
        repeated = {**passed, 'repeat': 2}
        self.assertTrue(experiment.infrastructure_failure(blocked))
        self.assertFalse(experiment.infrastructure_failure(passed))
        self.assertEqual(experiment.latest_jobs([blocked, passed, repeated]), [passed, repeated])

    def test_accounting_separates_selection_judgment_and_source_errors(self):
        case = {'id': 'synthetic', 'gold': [
            {'id': 'F001', 'relation': 'CONTRADICTED'},
            {'id': 'F002', 'relation': 'CONTRADICTED'},
            {'id': 'F003', 'relation': 'SUPPORTED'}]}
        job = {'case': 'synthetic', 'cap': 20, 'repeat': 1, 'success': True,
               'selectedGoldIds': ['F001', 'F003'], 'skipped': 1,
               'rows': [
                   {'goldId': 'F001', 'expectedRelation': 'CONTRADICTED', 'relation': 'SUPPORTED',
                    'sourceRef': 'E2', 'sourceConfidence': 1.0, 'factVerdictReviewFields': []},
                   {'goldId': 'F003', 'expectedRelation': 'SUPPORTED', 'relation': 'CONTRADICTED',
                    'sourceRef': 'E1', 'sourceConfidence': 1.0, 'factVerdictReviewFields': []}],
               'report': {'score': 3, 'needsReview': True}, 'elapsedSeconds': 2,
               'calls': [{'usage': {'input_tokens': 100, 'output_tokens': 10}}]}
        repeat = {**job, 'repeat': 2}
        result = experiment.summarize([case], [], [job, repeat])['caps'][1]
        self.assertEqual(result['selectionMissedLiveErrors'], 1)
        self.assertEqual(result['judgeMissedSelectedErrors'], 1)
        self.assertEqual(result['falseContradictions'], 1)
        self.assertEqual(result['threePointButFlagged'], 1)
        self.assertEqual(result['falseClearPasses'], 0)
        self.assertEqual(result['sourceLinkAgreement'], .5)
        self.assertFalse(result['wrongSourceLinks'][0]['flagged'])
        self.assertEqual(result['primaryUsage']['input_tokens'], 100)
        self.assertEqual(result['usage']['input_tokens'], 200)


if __name__ == '__main__':
    unittest.main()
