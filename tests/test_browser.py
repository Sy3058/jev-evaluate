import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from rendering import inspect_html
from server import AppHandler, init_db
from test_evaluation import fake_jev

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipUnless(sync_playwright, 'Playwright 미설치: 브라우저 검증 미실행')
class BrowserTests(unittest.TestCase):
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
                    browser=p.chromium.launch(headless=True,chromium_sandbox=True)
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
                        page.get_by_role('button',name='케이스 저장',exact=True).click()
                        expect(page.locator('#toast')).to_contain_text('저장했습니다')
                        page.get_by_role('button',name='결과 분석',exact=True).click()
                        page.locator('[data-evaluate]').click()
                        expect(page.locator('#result-rows')).to_contain_text('3 / 3')
                        page.locator('[data-history]').click()
                        expect(page.locator('#history-dialog')).to_be_visible()
                        expect(page.locator('#history-content')).to_contain_text('five-axis-v2')
                        page.locator('#close-history').click()
                        page.locator('[data-settings]').click()
                        page.locator('#edit-spec').get_by_text('추가 평가 기준 (선택)',exact=True).click()
                        page.locator('#edit-requirements').fill('변경된 요구사항')
                        page.locator('#settings-form').get_by_role('button',name='저장',exact=True).click()
                        expect(page.locator('#result-rows')).to_contain_text('설정 변경')
                        self.assertEqual(errors,[])
                    finally:
                        browser.close()
            finally:
                http.shutdown(); http.server_close(); thread.join()
