import unittest

from validate import metrics, wilson
import evaluation as rubric


class ValidationTests(unittest.TestCase):
    def run_case(self, score=3):
        return {'id':'x','split':'holdout','category':'코딩','caseHash':'hash',
                'report':{'axes':{a:{'status':'rated','score':score} for a in rubric.LABELS}}}

    def test_no_human_labels_never_passes(self):
        result=metrics([self.run_case()]*3,[])
        self.assertEqual(result['humanLabeledHoldoutCases'],0)
        self.assertFalse(result['automaticThresholdsMet'])
        self.assertEqual(result['repeatAgreement'],1)

    def test_repeats_do_not_inflate_human_sample_count(self):
        labels=[{'id':'x','reviewer':'human','humanReviewed':True,'caseHash':'hash','axes':{'truthfulness':3}}]
        result=metrics([self.run_case()]*3,labels)
        group=next(g for g in result['groups'] if g['category']=='코딩' and g['axis']=='truthfulness')
        self.assertEqual(group['n'],1)
        self.assertFalse(group['pass'])
        self.assertEqual(result['humanLabeledHoldoutCases'],1)

    def test_mismatched_input_or_ai_labels_excluded(self):
        for change in [{'caseHash':'different'},{'humanReviewed':False},{'reviewer':''}]:
            label={'id':'x','reviewer':'human','humanReviewed':True,'caseHash':'hash','axes':{'truthfulness':3},**change}
            self.assertEqual(metrics([self.run_case()],[label])['humanLabeledHoldoutCases'],0)

    def test_unknown_is_not_a_correct_zero(self):
        labels=[{'id':'x','reviewer':'human','humanReviewed':True,'caseHash':'hash','axes':{'truthfulness':0}}]
        result=metrics([self.run_case(None)],labels)
        group=next(g for g in result['groups'] if g['category']=='코딩' and g['axis']=='truthfulness')
        self.assertEqual(group['agreement'],0)
        self.assertEqual(group['coverage'],0)

    def test_small_sample_uncertainty_is_reported(self):
        low,high=wilson(1,1)
        self.assertLess(low,.3)
        self.assertEqual(high,1)
