import unittest
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from validate import main, metrics, wilson
import evaluation as rubric
from server import AppHandler, init_db
from test_evaluation import fake_jev
from validation.export_app_review import build_packet
from validation.recheck_app_stability import summarize


class ValidationTests(unittest.TestCase):
    def test_stability_summary_separates_score_and_claim_changes(self):
        base={'score':3,'status':'rated','claims':{'C1':'SUPPORTED'},
              'unverifiedCount':0,'skippedCandidateCount':0}
        changed_relation={**base,'claims':{'C1':'UNVERIFIED'}}
        changed_score={**base,'score':1,'lowConfidenceVerifiedCount':1}
        rows=[{'responseId':1,'runs':[base,changed_relation,base],'errors':[]},
              {'responseId':2,'runs':[base,changed_score,base],'errors':[]}]
        result=summarize(rows)
        self.assertEqual(result['claimRelationChangedResponses'],[1])
        self.assertEqual(result['scoreChangedResponses'],[2])
        self.assertEqual(result['lowConfidenceFlaggedResponses'],[2])
        self.assertEqual(result['scoreChangedWithLowConfidenceFlag'],[2])
        self.assertEqual(result['completeResponses'],2)

    def test_app_review_packet_hides_jev_claim_verdict_from_label_template(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path=Path(directory)/'review.db'
            init_db(db_path)
            handler=object.__new__(AppHandler)
            handler.db_path=db_path
            created=handler.create_case({'title':'review','category':'강의·첨부자료',
                'prompt':'행사 날짜를 알려줘.','attachmentText':'행사는 월요일입니다.',
                'responses':{'gpt-5.6-sol':'행사는 화요일입니다.'},
                'evaluationSpec':{'confirmed':True}})
            with patch('server.call_jev',side_effect=fake_jev):
                handler.evaluate(created['responses'][0]['id'])
            packet,labels=build_packet(db_path)
        self.assertEqual(len(labels),1)
        self.assertEqual(packet['runs'][0]['id'],labels[0]['id'])
        self.assertEqual(packet['runs'][0]['caseHash'],labels[0]['caseHash'])
        self.assertEqual(labels[0]['attachment'],'행사는 월요일입니다.')
        self.assertIsNone(labels[0]['claims']['C1']['relation'])
        self.assertFalse(labels[0]['humanReviewed'])

    def test_live_validation_uses_production_fact_path(self):
        fixture=[{'id':'fact_wrong','split':'holdout','category':'일반지식·설명',
                  'prompt':'무료 요금제의 내보내기를 설명해줘.',
                  'expectedAnswer':'무료 요금제에서는 파일을 내보낼 수 없습니다.',
                  'response':'무료 요금제에서는 파일을 내보낼 수 있습니다.',
                  'requirements':[]}]
        def judge(state,model,questions):
            answers={}
            for key,question in questions.items():
                if question['type']=='score':
                    answers[key]={'score':3.0,'probabilities':{'0':0.0,'1':0.0,'2':0.0,'3':1.0}}
                else:
                    preferred=('HIGH' if key.startswith('fact_kind_') else
                               'CONTRADICTED' if key.startswith('fact_relation_') else
                               'RATED' if key.startswith('status_') else
                               'MET' if key.startswith('requirement_') else
                               'A1' if key.startswith('answer_ref_') else 'E1')
                    answers[key]={'choice':preferred if preferred in question['criteria'] else 'NONE'}
            return {'model':model,'answers':answers}
        with tempfile.TemporaryDirectory() as directory:
            cases=Path(directory)/'cases.json'
            output=Path(directory)/'run.json'
            cases.write_text(json.dumps(fixture,ensure_ascii=False),encoding='utf-8')
            args=['validate.py','--live','--split','holdout','--repeats','1',
                  '--cases',str(cases),'--output',str(output)]
            with patch.object(sys,'argv',args), patch('server.call_jev',side_effect=judge), redirect_stdout(io.StringIO()):
                main()
            run=json.loads(output.read_text(encoding='utf-8'))['runs'][0]
            template=json.loads((Path(directory)/'cases.human-labels.template.json').read_text(encoding='utf-8'))
        self.assertEqual(run['report']['axes']['truthfulness']['score'],1)
        self.assertEqual(run['report']['factVerification']['majorCount'],1)
        self.assertIn('C1',template[0]['claims'])

    def run_case(self, score=3):
        return {'id':'x','split':'holdout','category':'코딩','caseHash':'hash',
                'report':{'axes':{a:{'status':'rated','score':score} for a in rubric.LABELS}}}

    def test_no_human_labels_never_passes(self):
        result=metrics([self.run_case()]*3,[])
        self.assertEqual(result['humanLabeledHoldoutCases'],0)
        self.assertFalse(result['automaticThresholdsMet'])
        self.assertEqual(result['repeatAgreement'],1)

    def test_claim_labels_measure_missed_and_false_contradictions(self):
        run=self.run_case()
        run['report']['factVerification']={'claims':[
            {'id':'C1','relation':'UNVERIFIED'}, {'id':'C2','relation':'CONTRADICTED'}]}
        labels=[{'id':'x','reviewer':'person','humanReviewed':True,'caseHash':'hash',
                 'axes':{},'claims':{'C1':{'relation':'CONTRADICTED'},
                                    'C2':{'relation':'SUPPORTED'}},'missingClaims':['빠진 사실 주장']}]
        found=metrics([run],labels)['factClaims']
        self.assertEqual(found['n'],2)
        self.assertEqual(found['missedContradictions'],1)
        self.assertEqual(found['falseContradictions'],1)
        self.assertEqual(found['unverifiedCount'],1)
        self.assertEqual(found['humanMissingClaims'],1)

    def test_claim_metrics_separate_false_clear_pass_from_review_flag(self):
        runs=[]; labels=[]
        for name,score,needs_review in [('clear',3,False),('review',3,True),
                                        ('caught',1,False),('abstained',None,True)]:
            run=self.run_case(score)
            run['id']=name
            run['report']['needsReview']=needs_review
            run['report']['factVerification']={'claims':[{'id':'C1','relation':'UNVERIFIED'}]}
            runs.append(run)
            labels.append({'id':name,'reviewer':'person','humanReviewed':True,'caseHash':'hash',
                           'axes':{},'claims':{'C1':{'relation':'CONTRADICTED'}}})
        result=metrics(runs,labels)['factClaims']
        self.assertEqual(result['casesWithHumanContradiction'],4)
        self.assertEqual(result['falseClearPasses'],1)
        self.assertEqual(result['falseClearPassRate'],.25)
        self.assertEqual(result['caseOutcomes'],{
            'caught':['caught'],'reviewFlagged':['review'],
            'falseClearPass':['clear'],'abstained':['abstained']})

    def test_missing_human_contradiction_counts_as_false_clear_pass(self):
        run=self.run_case(3)
        run['report']['needsReview']=False
        run['report']['factVerification']={'claims':[]}
        labels=[{'id':'x','reviewer':'person','humanReviewed':True,'caseHash':'hash',
                 'axes':{},'claims':{},'missingClaims':[
                     {'text':'자동 추출에서 빠진 거짓 주장','relation':'CONTRADICTED'}]}]
        result=metrics([run],labels)['factClaims']
        self.assertEqual(result['humanMissingClaims'],1)
        self.assertEqual(result['n'],1)
        self.assertEqual(result['missedContradictions'],1)
        self.assertEqual(result['contradictionRecall'],0)
        self.assertEqual(result['falseClearPasses'],1)
        self.assertEqual(result['caseOutcomes']['falseClearPass'],['x'])

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

    def test_fractional_score_uses_dominant_level_for_stage_agreement(self):
        labels=[{'id':'x','reviewer':'human','humanReviewed':True,'caseHash':'hash','axes':{'truthfulness':3}}]
        runs=[]
        for score in (2.60,2.90,2.75):
            run=self.run_case(score)
            run['report']['axes']['truthfulness']['dominantLevel']=3
            runs.append(run)
        result=metrics(runs,labels)
        group=next(g for g in result['groups'] if g['category']=='코딩' and g['axis']=='truthfulness')
        self.assertEqual(group['agreement'],1)
        self.assertEqual(result['repeatScoreRangeMax'],.3)
