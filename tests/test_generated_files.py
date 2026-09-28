import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from pypdf import PdfWriter

import bot_evaluation
from generated_files import MAX_FILE_BYTES, inspect_generated_file
from server import AppHandler, init_db
from test_bot_evaluation import bot_jev


class GeneratedFileTests(unittest.TestCase):
    def test_html_pdf_xlsx_extraction_and_invalid_inputs(self):
        html = b'<!DOCTYPE html><html lang="ko"><body>report</body></html>'
        found = inspect_generated_file('report.html', html)
        self.assertEqual(found['format'], 'html')
        self.assertIn('report', found['extractedText'])
        self.assertEqual(len(found['sha256']), 64)

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        pdf = io.BytesIO()
        writer.write(pdf)
        pdf_found = inspect_generated_file('report.pdf', pdf.getvalue())
        self.assertEqual(pdf_found['format'], 'pdf')
        self.assertIn('텍스트를 추출하지 못했습니다', pdf_found['extractionNote'])

        workbook = Workbook()
        workbook.active.title = '평가'
        workbook.active['A1'] = '스킬'
        workbook.active['B1'] = 3
        xlsx = io.BytesIO()
        workbook.save(xlsx)
        xlsx_found = inspect_generated_file('scores.xlsx', xlsx.getvalue())
        self.assertIn('[시트: 평가]', xlsx_found['extractedText'])
        self.assertIn('A1=스킬', xlsx_found['extractedText'])
        self.assertIn('B1=3', xlsx_found['extractedText'])

        for name, content in [('bad.exe', html), ('../bad.html', html), ('bad.pdf', b'not pdf')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                inspect_generated_file(name, content)
        with self.assertRaises(ValueError):
            inspect_generated_file('huge.html', b'x' * (MAX_FILE_BYTES + 1))

    def test_upload_changes_result_staleness_and_bot_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = object.__new__(AppHandler)
            handler.db_path = Path(directory) / 'test.db'
            init_db(handler.db_path)
            bot = handler.create_bot({'name': 'HTML Bot', 'description': '리포트',
                                      'instructions': 'HTML 파일을 생성한다.'})
            case = handler.create_case({'title': 'HTML', 'category': '강의·첨부자료', 'prompt': '리포트 작성',
                                        'evaluationMode': 'ai_bot', 'botId': bot['id'],
                                        'checkFocus': 'HTML 파일 생성', 'expectedBehavior': 'HTML 파일 생성',
                                        'evaluationSpec': {'outputFormat': 'text'},
                                        'responses': {'gpt-5.6-sol': '완성 파일을 첨부합니다.'}})
            response_id = case['responses'][0]['id']
            first = handler.save_artifact(response_id, 'report.html',
                b'<!DOCTYPE html><html lang="ko"><body>first</body></html>')
            self.assertEqual(first['format'], 'html')
            with (patch('server.inspect_html', return_value={'rendered': False, 'reason': '테스트'}),
                  patch('server.call_jev', side_effect=lambda state, model, questions:
                        self._judge(state, model, questions))):
                report = handler.evaluate(response_id)['report']
            self.assertEqual(report['botAssessment']['expectedResult']['verdict'], 'COMPLIANT')
            self.assertEqual(report['artifactSha256'], first['sha256'])
            self.assertIn('blocks', report['inspection'])
            self.assertFalse(handler.list_results('ai_bot')['items'][0]['stale'])
            handler.save_artifact(response_id, 'report.html',
                b'<!DOCTYPE html><html lang="ko"><body>second</body></html>')
            result = handler.list_results('ai_bot')['items'][0]
            self.assertTrue(result['stale'])
            self.assertIn('생성 파일 변경', result['staleReasons'])
            self.assertEqual(result['artifact']['filename'], 'report.html')

    def _judge(self, state, model, questions):
        self.assertIn('generated_artifact', state)
        self.assertEqual(state['output_format'], 'html')
        self.assertIn('first', '\n'.join(state['answer'].values()))
        self.assertIn('first', state['visible_report_text'])
        self.assertNotIn('<html', state['visible_report_text'])
        self.assertNotIn('blocks', state['inspection'])
        self.assertFalse(state['file_delivery_missing'])
        self.assertTrue(state['file_delivery_confirmed'])
        self.assertIn('다운로드할 수 있다', state['artifact_observation'])
        self.assertIn('파일 다운로드를 제공했다는 증거', state['artifact_observation'])
        self.assertIn('업로드 파일 자체의 형식', questions['bot_if_observation_1']['instructions'])
        self.assertIn('FILE_METADATA', questions['bot_if_observation_1']['criteria'])
        self.assertIn('HTML 태그·CSS', questions['bot_issue_response_length_1']['instructions'])
        self.assertNotIn('instruction_following', questions)
        self.assertNotIn('source_ref_response_length', questions)
        self.assertNotIn('state.attachment_text', questions['bot_if_claim_1']['instructions'])
        return bot_jev(state, model, questions)


if __name__ == '__main__':
    unittest.main()
