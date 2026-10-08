import os
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import evaluation as rubric
from rendering import inspect_html
from server import AppHandler, init_db
from test_evaluation import fake_jev
from test_bot_evaluation import bot_jev

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright 미설치: 브라우저 검증 미실행')
class BrowserTests(unittest.TestCase):
    def test_local_truth_review_saves_blind_human_labels(self):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path(__file__).resolve().parents[1]/'.browsers'))
        html=Path(__file__).resolve().parents[1]/'validation/review_labels.html'
        labels=[{'id':'response-1-evaluation-2','caseHash':'hash','title':'검토 사례',
                 'category':'일반지식·설명','model':'test','prompt':'날짜는?',
                 'answer':'행사는 화요일입니다.','attachment':'행사는 월요일입니다.',
                 'reviewer':'','humanReviewed':False,'axes':{'truthfulness':None},
                 'claims':{'C1':{'text':'행사는 화요일입니다.','relation':None,'importance':None}},
                 'missingClaims':[]}]
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            try:
                page=browser.new_page(accept_downloads=True)
                page.goto(html.as_uri())
                page.locator('#source').set_input_files({'name':'labels.json','mimeType':'application/json',
                    'buffer':json.dumps(labels,ensure_ascii=False).encode('utf-8')})
                expect(page.locator('#prompt')).to_have_text('날짜는?')
                expect(page.locator('#attachment')).to_have_text('행사는 월요일입니다.')
                page.locator('#completed').click()
                expect(page.locator('#completed')).not_to_be_checked()
                page.locator('#reviewer').fill('검토자')
                page.locator('.claim-controls select').nth(0).select_option('CONTRADICTED')
                page.locator('.claim-controls select').nth(1).select_option('HIGH')
                page.locator('#missing').fill('추출에서 빠진 거짓 주장')
                page.locator('#completed').click()
                expect(page.locator('#completed')).not_to_be_checked()
                page.locator('#missing').fill('CONTRADICTED | 추출에서 빠진 거짓 주장')
                page.locator('#completed').check()
                expect(page.locator('#progress')).to_contain_text('검토 완료 1건')
                with page.expect_download() as download_info:
                    page.locator('#download').click()
                with tempfile.TemporaryDirectory() as directory:
                    target=Path(directory)/'labels.json'
                    download_info.value.save_as(target)
                    saved=json.loads(target.read_text(encoding='utf-8'))
                self.assertEqual(saved[0]['claims']['C1']['relation'],'CONTRADICTED')
                self.assertEqual(saved[0]['claims']['C1']['importance'],'HIGH')
                self.assertEqual(saved[0]['missingClaims'],[
                    {'relation':'CONTRADICTED','text':'추출에서 빠진 거짓 주장'}])
                self.assertTrue(saved[0]['humanReviewed'])
            finally:
                browser.close()

    def test_jev_project_guide_replays_offline_file(self):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parents[1]/'.browsers'))
        html = Path(__file__).resolve().parents[1] / 'static' / 'jev-project-guide.html'
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                requests = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url))
                page.goto(html.as_uri())
                expect(page.get_by_role('heading', name='등록부터 결과 화면까지')).to_be_visible()
                expect(page.get_by_role('heading', name='JEV에는 무엇을 묻나?')).to_be_visible()
                expect(page.locator('#response-tabs button')).to_have_count(6)
                expect(page.locator('#response-content')).to_contain_text("'탁월(Excellent)'")
                expect(page.locator('#response-file-status')).to_contain_text('업로드 없음')
                page.get_by_role('button', name='gpt-5.6-sol', exact=True).click()
                expect(page.locator('#response-content')).to_contain_text('리포트 다운로드')
                expect(page.locator('#response-file-status')).to_contain_text('업로드됨')
                expect(page.locator('#response-report-excerpt')).to_contain_text('재진술의 정확성')
                expect(page.locator('#step-title')).to_contain_text('요청·기대 결과')
                page.get_by_role('button', name='A2', exact=True).click()
                expect(page.locator('#reference-detail')).to_contain_text('탁월(Excellent)')
                page.get_by_role('button', name='inspection', exact=True).click()
                expect(page.locator('#reference-detail')).to_contain_text('null')
                page.get_by_role('button', name='다음 단계').click()
                expect(page.locator('#step-title')).to_contain_text('답변·자료 위치')
                page.get_by_role('button', name='다음 단계').click()
                expect(page.locator('#step-title')).to_contain_text('JEV Choice 판정')
                expect(page.locator('#stage')).to_contain_text('70% / 65%')
                expect(page.locator('#stage')).to_contain_text('87% / 86%')
                expect(page.locator('#choice-bars .chart-row')).to_have_count(7)
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('CONTRADICTION')
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('70%')
                expect(page.locator('#choice-bars')).to_contain_text('지침이 요구하거나 금지한 행동과 실제 내용이 충돌')
                page.locator('#chart-question').select_option('bot_if_applicability_2')
                expect(page.locator('#choice-bars .chart-row')).to_have_count(3)
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('NOT_APPLICABLE')
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('52%')
                page.locator('#chart-question').select_option('bot_if_requirement_1')
                expect(page.locator('#choice-bars .chart-row')).to_have_count(107)
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('X1')
                expect(page.locator('#choice-bars .chart-row.picked')).to_contain_text('App 지침을 우선')
                page.locator('#choice-details summary').click()
                expect(page.locator('#choice-rows tr')).to_have_count(18)
                expect(page.locator('#choice-rows')).to_contain_text('NOT_APPLICABLE')
                page.get_by_role('button', name='전체 재생').click()
                expect(page.locator('#step-title')).to_contain_text('리포트·이력 저장')
                page.get_by_role('button', name='앱의 근거 검사·점수').click()
                expect(page.locator('#stage')).to_contain_text('0 / 3')
                expect(page.locator('#stage')).to_contain_text('FILE_ABSENT')
                self.assertEqual(errors, [])
                self.assertEqual(requests, [html.as_uri()])
            finally:
                browser.close()

    def test_ai_bot_create_evaluate_and_version(self):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path(__file__).resolve().parents[1]/'.browsers'))
        with tempfile.TemporaryDirectory() as directory:
            class Handler(AppHandler):
                db_path=Path(directory)/'test.db'
                def log_message(self,*args): pass
            init_db(Handler.db_path)
            http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
            try:
                with patch('server.call_jev',side_effect=lambda *args: bot_jev(*args,verdict='VIOLATED')), sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    try:
                        page=browser.new_page()
                        errors=[]; page.on('pageerror',lambda e: errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}')
                        page.wait_for_function("() => Boolean(config?.criteria)")
                        self.assertEqual(page.evaluate("""() => {
                            mode = 'ai_bot';
                            return resultStatus({report:{needsReview:true,
                                botAssessment:{status:'compliant'}}});
                        }"""),'지침·기대 결과 충족 · 검토 필요')
                        fact_html=page.evaluate("""() => reportDetails({
                            scoreType:'expected_level_0_3',
                            axes:{truthfulness:{score:1,status:'rated',description:'근거 대조',
                                scoringMethod:'confirmed_claim_contradictions',needsReview:true,notes:[],
                                answerRef:'C1',answerText:'잘못된 사실',sourceRef:'E1',sourceText:'확인한 원문'}},
                            factVerification:{score:1,status:'rated',claimCount:1,verifiedCount:1,
                                unverifiedCount:0,lowConfidenceVerifiedCount:1,skippedCandidateCount:0,
                                claims:[{id:'C1',text:'잘못된 사실',importance:'HIGH',
                                    relation:'CONTRADICTED',sourceRef:'E1',sourceText:'확인한 원문',
                                    importanceConfidence:.92,relationConfidence:.41,sourceConfidence:.96,
                                    lowConfidenceFields:['relationConfidence']}]},
                            requirements:[],checks:[],issues:[],sourceMetadata:{}
                        })""")
                        self.assertIn('핵심 주장 모순',fact_html)
                        self.assertIn('사실성 판정 검토 1개',fact_html)
                        self.assertIn('사실성 판정 검토 필요',fact_html)
                        self.assertIn('관계 0.41',fact_html)
                        self.assertEqual(fact_html.count('확인한 원문'),1)
                        self.assertNotIn('Truthfulness 근거 대조',fact_html)
                        self.assertEqual(fact_html.count('Truthfulness:'),1)
                        ordered_html=page.evaluate("""() => reportDetails({
                            scoreType:'expected_level_0_3',
                            axes:{truthfulness:{score:2,status:'rated',description:'근거 대조',
                                scoringMethod:'confirmed_claim_contradictions',needsReview:true,notes:[]}},
                            factVerification:{score:2,status:'rated',claimCount:3,verifiedCount:2,
                                unverifiedCount:1,majorCount:0,minorCount:1,claims:[
                                    {id:'C1',text:'정상 주장',importance:'HIGH',relation:'SUPPORTED',sourceRef:'E1',sourceText:'정상 근거'},
                                    {id:'C2',text:'틀린 주장',importance:'LOW',relation:'CONTRADICTED',sourceRef:'E2',sourceText:'반박 근거'},
                                    {id:'C3',text:'미확인 주장',importance:'HIGH',relation:'UNVERIFIED',sourceRef:'NONE'}]},
                            requirements:[],checks:[],issues:[],sourceMetadata:{}
                        })""")
                        self.assertLess(ordered_html.index('틀린 주장'),ordered_html.index('정상 주장'))
                        self.assertIn('검토가 필요한 주장 1개 보기',ordered_html)
                        self.assertIn('나머지 주장 1개 보기',ordered_html)
                        allocation_html=page.evaluate("""() => reportDetails({
                            scoreType:'expected_level_0_3',
                            axes:{truthfulness:{score:2,status:'rated',description:'근거 대조',
                                scoringMethod:'confirmed_claim_contradictions',needsReview:true,notes:[]}},
                            factVerification:{score:2,status:'rated',claimCount:2,verifiedCount:2,
                                unverifiedCount:0,majorCount:0,minorCount:1,
                                scoreAllocationReviewCount:1,factVerdictReviewCount:0,claims:[
                                    {id:'C1',text:'일치 주장',importance:'HIGH',relation:'SUPPORTED',
                                     sourceRef:'E1',sourceText:'일치 근거',importanceConfidence:.44,
                                     relationConfidence:.99,sourceConfidence:1,lowConfidenceFields:[],
                                     scoreAllocationReviewFields:[],factVerdictReviewFields:[]},
                                    {id:'C2',text:'모순 주장',importance:'LOW',relation:'CONTRADICTED',
                                     sourceRef:'E2',sourceText:'반박 근거',importanceConfidence:.51,
                                     relationConfidence:.98,sourceConfidence:.99,
                                     lowConfidenceFields:['importanceConfidence'],
                                     scoreAllocationReviewFields:['importanceConfidence'],factVerdictReviewFields:[]}]},
                            requirements:[],checks:[],issues:[],sourceMetadata:{}
                        })""")
                        self.assertIn('2 / 3 · 점수 배점 검토 필요',allocation_html)
                        self.assertNotIn('사실성 판정 검토 필요',allocation_html)
                        self.assertIn('나머지 주장 1개 보기',allocation_html)
                        self.assertIn('이전 기준 검토 필요',page.evaluate("""() => axisCell(
                            {score:3,scoringMethod:'confirmed_claim_contradictions',needsReview:true},
                            'expected_level_0_3',
                            {claims:[{relation:'SUPPORTED',lowConfidenceFields:['importanceConfidence']}]})"""))
                        unknown_html=page.evaluate("""() => reportDetails({
                            scoreType:'expected_level_0_3',
                            axes:{truthfulness:{score:null,status:'unverifiable',description:'판정 보류',
                                scoringMethod:'confirmed_claim_contradictions',needsReview:true,notes:[]}},
                            factVerification:{score:null,status:'unverifiable',claimCount:0,verifiedCount:0,
                                unverifiedCount:0,lowConfidenceExcludedCount:1,skippedCandidateCount:0,
                                claims:[{id:'C1',text:'행사는 화요일입니다.',
                                    importance:'NONE',relation:'NOT_APPLICABLE',sourceRef:'NONE',
                                    importanceConfidence:.38,lowConfidenceFields:['importanceConfidence']}]},
                            requirements:[],checks:[],issues:[],sourceMetadata:{}
                        })""")
                        self.assertIn('사실 주장 여부 검토 1개',unknown_html)
                        expect(page.locator('#bot-select-wrap')).to_have_count(0)
                        page.get_by_role('button',name='AI Bot 평가').click()
                        expect(page.locator('#bot-select-wrap')).to_be_visible()
                        expect(page.locator('#input-mode-heading')).to_have_text('AI Bot 평가 질문')
                        page.get_by_role('button',name='모델 평가').click()
                        expect(page.locator('#bot-select-wrap')).to_have_count(0)
                        expect(page.locator('#input-mode-heading')).to_have_text('모델 평가 질문')
                        page.locator('[name=title]').fill('모델 전환 검증')
                        page.locator('[name=prompt]').fill('2+2는?')
                        page.locator('[name="response:gpt-5.6-sol"]').fill('4')
                        page.get_by_role('button',name='케이스 저장').click()
                        expect(page.locator('#toast')).to_contain_text('저장했습니다')
                        model_items=page.request.get(f'http://127.0.0.1:{http.server_port}/api/results?mode=model').json()['items']
                        self.assertEqual(model_items[0]['evaluationMode'],'model')
                        self.assertIsNone(model_items[0]['botId'])
                        page.get_by_role('button',name='AI Bot 평가').click()
                        page.get_by_role('button',name='AI Bot 관리').click()
                        page.locator('#bot-form [name=name]').fill('상담 Bot')
                        page.locator('#bot-form [name=description]').fill('인사하는 Bot')
                        page.locator('#bot-form [name=instructions]').fill('먼저 인사한다.')
                        page.locator('#bot-form button[type=submit]').click()
                        expect(page.locator('#bot-list')).to_contain_text('상담 Bot')
                        page.get_by_role('button',name='답변 입력').click()
                        page.locator('#bot-select').select_option(label='상담 Bot · v1')
                        expect(page.locator('#bot-instruction-preview')).to_contain_text('먼저 인사한다.')
                        page.locator('[name=title]').fill('Bot 검증')
                        page.locator('[name=prompt]').fill('도움말을 알려줘')
                        page.locator('#case-form [name=checkFocus]').fill('인사 여부')
                        page.locator('#case-form [name=expectedBehavior]').fill('먼저 인사한다.')
                        page.locator('[name="response:gpt-5.6-sol"]').fill('도움말입니다.')
                        page.locator('[data-artifact-model="gpt-5.6-sol"]').set_input_files({
                            'name':'코칭.html','mimeType':'text/html',
                            'buffer':b'<!DOCTYPE html><html lang="ko"><body>report</body></html>'})
                        page.get_by_role('button',name='케이스 저장').click()
                        page.get_by_role('button',name='결과 분석').click()
                        expect(page.locator('#result-rows .case-row')).to_contain_text('상담 Bot · 지침 v1')
                        artifact_item=page.request.get(f'http://127.0.0.1:{http.server_port}/api/results?mode=ai_bot').json()['items'][0]
                        self.assertEqual(artifact_item['artifact']['filename'],'코칭.html')
                        download_url=f'http://127.0.0.1:{http.server_port}'+artifact_item['artifact']['downloadUrl']
                        self.assertIn(b'report',page.request.get(download_url).body())
                        page.locator('[data-evaluate-case]').click()
                        expect(page.locator('#result-rows .case-row')).to_contain_text('지침 위반')
                        if page.locator('#result-rows .case-details').get_attribute('hidden') is not None:
                            page.locator('[data-case-toggle]').click()
                        expect(page.locator('.model-results th')).to_have_count(8)
                        expect(page.locator('.scenario-summary')).to_contain_text('인사 여부')
                        expect(page.locator('.scenario-summary')).to_contain_text('먼저 인사한다.')
                        expect(page.locator('.model-evidence')).to_contain_text('코칭.html')
                        expect(page.locator('.bot-assessment')).not_to_have_attribute('open', '')
                        page.locator('.bot-assessment summary').click()
                        expect(page.locator('.bot-assessment')).to_contain_text('먼저 인사한다.')
                        old_axis = {'score': 2, 'description': '경미한 위반 1건이 확인됐습니다.',
                                    'needsReview': True, 'sourceRef': 'B4', 'pendingReasons': [],
                                    'rejectedFindings': [], 'notes': [], 'violations': [{
                                        'severity': 'minor', 'label': '지침의 필수 구성 요소가 빠짐',
                                        'answerRef': 'A3', 'answerText': 'body{font-size:13px}<body>리포트</body>',
                                        'sourceRef': 'B4', 'sourceText': '# 3. 발화 인용\n- 원문 인용\n- 타임스탬프',
                                        'confidence': .19}]}
                        rendered_issue = page.evaluate("axis => qualityAxis('instruction_following', axis, {scoreType:'violation_count_0_3', botAssessment:{rules:[{id:'B4',verdict:'UNKNOWN'}]}})", old_axis)
                        self.assertIn('기존 점수의 근거 검토 필요', rendered_issue)
                        self.assertIn('어떤 항목을 어겼는지는 기록되지 않았습니다', rendered_issue)
                        self.assertIn('HTML/CSS 원본', rendered_issue)
                        self.assertIn('<details class="quality-axis" open>', rendered_issue)
                        pending_axis = {'score': None, 'status': 'unverifiable',
                                        'description': '근거 확인이 필요해 점수를 보류했습니다.',
                                        'needsReview': True, 'violations': [], 'notes': [],
                                        'pendingReasonChoices': [{
                                            'code': 'ISSUE_ORDER_CONFLICT', 'label': '위반 선택 순서 충돌',
                                            'detail': '첫 번째 위반 없음, 두 번째 위반 경미 누락', 'origin': 'validator'}],
                                        'judgeUnverifiableReason': {'code': 'NONE',
                                            'label': '평가 가능한 답변과 기준이 있음', 'confidence': .91}}
                        rendered_pending = page.evaluate("axis => qualityAxis('instruction_following', axis, {scoreType:'violation_count_0_3'})", pending_axis)
                        self.assertIn('판정 불가 이유 · 자동 검사 선택 1개', rendered_pending)
                        self.assertIn('위반 선택 순서 충돌', rendered_pending)
                        self.assertIn('JEV 자료 관측 선택', rendered_pending)
                        linked_issue = page.evaluate("issue => issueEvidence(issue)", {
                            'severity': 'minor', 'label': '필수 구성 요소 누락',
                            'requirementRef': 'R2', 'requirementSection': '출력 형식',
                            'requirementText': '- 표로 작성한다.',
                            'observationRef': 'O3', 'observedText': '문단으로 작성했습니다.',
                            'confidence': .18})
                        self.assertIn('적용 지침', linked_issue)
                        self.assertIn('표로 작성한다', linked_issue)
                        self.assertIn('문단으로 작성했습니다', linked_issue)
                        self.assertNotIn('위반 선택의 확신도가 낮습니다', linked_issue)
                        page.locator('[data-upload-for]').set_input_files({
                            'name':'수정.html','mimeType':'text/html',
                            'buffer':b'<!DOCTYPE html><html lang="ko"><body>updated</body></html>'})
                        expect(page.locator('#result-rows .case-details')).to_contain_text('생성 파일 변경')
                        page.get_by_role('button',name='AI Bot 관리').click()
                        page.locator('[data-edit-bot]').click()
                        page.locator('#bot-form [name=instructions]').fill('한 문장으로 답한다.')
                        page.locator('#bot-form button[type=submit]').click()
                        expect(page.locator('#bot-list')).to_contain_text('v2')
                        page.get_by_role('button',name='결과 분석').click()
                        expect(page.locator('#result-rows .case-row')).to_contain_text('지침 v1')
                        page.locator('[data-update-bot-version]').click()
                        expect(page.locator('#result-rows .case-row')).to_contain_text('재평가 필요')
                        expect(page.locator('#result-rows .case-details')).to_contain_text('Bot 지침 버전 변경')
                        self.assertEqual(errors,[])
                    finally:
                        browser.close()
            finally:
                http.shutdown(); http.server_close(); thread.join()

    def test_render_hidden_content_and_no_script_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            result=inspect_html('<p>Visible</p><p style="display:none">Hidden</p>'
                '<script>document.body.textContent="executed"</script>',Path(directory))
            self.assertTrue(result['rendered'],result)
            self.assertIn('Visible',result['observations']['visibleText'])
            self.assertNotIn('Hidden',result['observations']['visibleText'])
            self.assertNotIn('executed',result['observations']['visibleText'])
            self.assertIn('Hidden',result['observations']['hiddenBlocks'])
            self.assertTrue((Path(directory)/result['screenshot']).is_file())

    def test_form_evaluate_history_and_settings(self):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(Path(__file__).resolve().parents[1]/'.browsers'))
        with tempfile.TemporaryDirectory() as directory:
            class Handler(AppHandler):
                db_path=Path(directory)/'test.db'
                def log_message(self,*args): pass
            init_db(Handler.db_path)
            http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
            try:
                with patch('server.call_jev',side_effect=fake_jev), sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    try:
                        page=browser.new_page()
                        errors=[]; page.on('pageerror',lambda e: errors.append(str(e)))
                        page.goto(f'http://127.0.0.1:{http.server_port}')
                        page.locator('[name=title]').fill('브라우저 검증')
                        page.locator('#case-form [name=category]').select_option('추론·문제해결')
                        page.locator('[name=prompt]').fill('2+2의 답만 출력해줘')
                        expect(page.locator('#new-requirements')).to_be_hidden()
                        expect(page.locator('#new-confirmed')).to_have_count(0)
                        page.locator('[name="response:gpt-5.6-sol"]').fill('4')
                        page.locator('[name="response:gpt-5.6-terra"]').fill('4')
                        page.get_by_role('button',name='케이스 저장',exact=True).click()
                        expect(page.locator('#toast')).to_contain_text('저장했습니다')
                        page.get_by_role('button',name='결과 분석',exact=True).click()
                        expect(page.locator('#result-rows .case-row')).to_have_count(1)
                        expect(page.locator('#result-rows .case-details')).to_be_hidden()
                        page.locator('[data-case-toggle]').click()
                        expect(page.locator('#result-rows .model-results tbody tr.model-evidence')).to_have_count(2)
                        page.locator('[data-evaluate-case]').click()
                        expect(page.locator('#result-rows .case-row')).to_contain_text('2/2개 현재 평가')
                        expect(page.locator('#result-rows .case-details')).to_be_visible()
                        expect(page.locator('#result-rows')).to_contain_text('3.00 / 3')
                        expect(page.locator('#result-rows .quality-overview')).to_have_count(2)
                        expect(page.locator('#result-rows .quality-overview .quality-axis')).to_have_count(10)
                        expect(page.locator('#result-rows')).to_contain_text('모델 품질 분석')
                        page.locator('[data-history]').first.click()
                        expect(page.locator('#history-dialog')).to_be_visible()
                        expect(page.locator('#history-content')).to_contain_text(rubric.VERSION)
                        expect(page.locator('#history-content .quality-axis')).to_have_count(5)
                        page.locator('#close-history').click()
                        page.locator('[data-settings]').click()
                        page.locator('#edit-spec').get_by_text('추가 평가 기준 (선택)',exact=True).click()
                        page.locator('#edit-requirements').fill('변경된 요구사항')
                        page.locator('#settings-form').get_by_role('button',name='저장',exact=True).click()
                        expect(page.locator('#result-rows')).to_contain_text('공통 설정 변경')
                        self.assertEqual(errors,[])
                    finally:
                        browser.close()
            finally:
                http.shutdown(); http.server_close(); thread.join()
