"""Exercise request boundaries over HTTP without using the Jev API."""
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import request_security as security
import server


class RequestSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        db = Path(self.directory.name) / 'test.db'
        server.init_db(db)
        class Handler(server.AppHandler):
            db_path = db
            csrf_token = 'test-only-token'
            def log_message(self, *args):
                pass
        self.handler = Handler
        self.http = security.LocalHTTPServer(('127.0.0.1', 0), Handler, max_connections=2)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.authority = f'127.0.0.1:{self.http.server_port}'

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.directory.cleanup()

    def request(self, method='POST', path='/api/requirements/draft', headers=None, body=b'{}'):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            default = {'Content-Type': 'application/json', 'X-JEV-CSRF-Token': self.handler.csrf_token}
            default.update(headers or {})
            conn.request(method, path, body=None if method == 'GET' else body, headers=default)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_config_and_valid_same_origin_post(self):
        status, raw = self.request('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['csrfToken'], self.handler.csrf_token)
        status, _ = self.request(headers={'Origin': f'http://{self.authority}'})
        self.assertEqual(status, 200)

    def test_untrusted_host_and_origin_are_rejected(self):
        for headers in ({'Host': 'attacker.invalid'}, {'Host': f'attacker.invalid:{self.http.server_port}'},
                        {'Host': '127.0.0.1:1'}, {'Origin': 'https://attacker.invalid'},
                        {'Origin': 'null'}, {'Origin': f'http://localhost:{self.http.server_port}'}):
            with self.subTest(headers=headers):
                status, _ = self.request('GET', '/api/config', headers=headers)
                self.assertEqual(status, 403)

    def test_token_is_required_before_evaluation(self):
        for token in ('', 'wrong-token'):
            with patch.object(self.handler, 'evaluate') as evaluate:
                status, _ = self.request(path='/api/responses/1/evaluate',
                                         headers={'X-JEV-CSRF-Token': token, 'Content-Length': '2'}, body=b'')
                self.assertEqual(status, 403)
                evaluate.assert_not_called()

    def test_text_plain_and_invalid_lengths_are_rejected(self):
        status, _ = self.request(headers={'Content-Type': 'text/plain', 'Content-Length': '2'}, body=b'')
        self.assertEqual(status, 415)
        for value in ('-1', '0', 'invalid', '999999999999999999999999999999'):
            with self.subTest(length=value):
                status, _ = self.request(headers={'Content-Length': value}, body=b'')
                self.assertEqual(status, 400)
        status, _ = self.request(headers={'Content-Length': str(security.MAX_JSON_BYTES + 1)}, body=b'')
        self.assertEqual(status, 413)
        status, _ = self.request(headers={'Transfer-Encoding': 'chunked'}, body=b'')
        self.assertEqual(status, 400)

    def test_duplicate_headers_and_missing_length_are_rejected(self):
        for duplicate in ('Host', 'Origin', 'Content-Length', 'Content-Type', 'X-JEV-CSRF-Token'):
            conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
            try:
                conn.putrequest('POST', '/api/requirements/draft', skip_host=True)
                values = {'Host': self.authority, 'Origin': f'http://{self.authority}',
                          'Content-Length': '2', 'Content-Type': 'application/json',
                          'X-JEV-CSRF-Token': self.handler.csrf_token}
                for name, value in values.items():
                    conn.putheader(name, value)
                    if name == duplicate:
                        conn.putheader(name, value)
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                conn.close()
        headers = Message()
        headers['Content-Type'] = 'application/json'
        obj = SimpleNamespace(headers=headers)
        with self.assertRaises(security.RequestRejected):
            security.read_body(obj, security.MAX_JSON_BYTES, 'application/json')

    def test_incomplete_body_and_read_timeout(self):
        conn = socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3)
        try:
            request = (f'POST /api/requirements/draft HTTP/1.1\r\nHost: {self.authority}\r\n'
                       'Content-Type: application/json\r\nContent-Length: 100\r\n'
                       f'X-JEV-CSRF-Token: {self.handler.csrf_token}\r\n\r\n{{}}').encode()
            conn.sendall(request)
            conn.shutdown(socket.SHUT_WR)
            self.assertIn(b' 400 ', conn.recv(4096).split(b'\r\n')[0])
        finally:
            conn.close()
        with patch.object(security, 'READ_TIMEOUT_SECONDS', .1):
            status, _ = self.request(headers={'Content-Length': '100'}, body=b'{}')
            self.assertEqual(status, 408)

    def test_total_body_deadline_is_enforced(self):
        headers = Message()
        headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = '2'
        obj = SimpleNamespace(headers=headers, connection=Mock(), rfile=Mock())
        obj.rfile.read1.return_value = b'{'
        with patch.object(security.time, 'monotonic', side_effect=[0, 0, security.READ_TIMEOUT_SECONDS + 1]):
            with self.assertRaises(security.RequestRejected) as caught:
                security.read_body(obj, 10, 'application/json')
        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(obj.rfile.read1.call_count, 1)

    def test_slow_request_line_and_headers_have_a_total_deadline(self):
        for prefix in (b'GET /api/con', b'GET /api/config HTTP/1.1\r\nHost: '):
            with self.subTest(prefix=prefix), patch.object(security, 'HEADER_TIMEOUT_SECONDS', .2):
                logs = []
                with patch.object(self.handler, 'log_message', lambda h, fmt, *args: logs.append(fmt % args)):
                    conn = socket.create_connection(('127.0.0.1', self.http.server_port), timeout=1)
                    try:
                        started = time.monotonic()
                        conn.sendall(prefix)
                        for _ in range(8):
                            time.sleep(.05)
                            try:
                                conn.sendall(b'1')
                            except OSError:
                                break
                        try:
                            self.assertEqual(conn.recv(4096), b'')
                        except (ConnectionResetError, ConnectionAbortedError):
                            pass  # Windows may reset a closed connection with unread data.
                        self.assertLess(time.monotonic() - started, .9)
                        self.assertTrue(any('timed out' in line for line in logs))
                    finally:
                        conn.close()
        self.assertEqual(self.request('GET', '/api/config')[0], 200)

    def test_html_and_json_responses_block_framing(self):
        for method, path in (('GET', '/'), ('HEAD', '/'), ('GET', '/api/config'), ('GET', '/missing-page')):
            with self.subTest(method=method, path=path):
                conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
                try:
                    conn.request(method, path)
                    response = conn.getresponse()
                    self.assertEqual(response.getheader('Content-Security-Policy'), "frame-ancestors 'none'")
                    self.assertEqual(response.getheader('X-Frame-Options'), 'DENY')
                    response.read()
                finally:
                    conn.close()

    def test_browser_blocks_app_inside_another_origin_frame(self):
        import os
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is not installed')
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(server.ROOT / '.browsers'))
        target = f'http://{self.authority}/'
        class OuterHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = f'<iframe name="target" src="{target}"></iframe>'.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        outer = ThreadingHTTPServer(('127.0.0.1', 0), OuterHandler)
        thread = threading.Thread(target=outer.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    with page.expect_console_message(predicate=lambda message: 'frame-ancestors' in message.text):
                        page.goto(f'http://127.0.0.1:{outer.server_port}/')
                    self.assertEqual(page.frame_locator('iframe').locator('#case-form').count(), 0)
                finally:
                    browser.close()
        finally:
            outer.shutdown()
            outer.server_close()
            thread.join(3)

    def test_upload_retains_size_limit_and_requires_token(self):
        created = object.__new__(self.handler).create_case({
            'title': 'test', 'category': server.rubric.CATEGORIES[2], 'prompt': 'test',
            'responses': {server.MODELS[0]: 'test'}})
        path = f"/api/responses/{created['responses'][0]['id']}/artifact"
        headers = {'Content-Type': 'application/octet-stream', 'X-File-Name': 'test.html'}
        status, _ = self.request(path=path, headers={**headers, 'X-JEV-CSRF-Token': ''}, body=b'<p>test</p>')
        self.assertEqual(status, 403)
        status, _ = self.request(path=path, headers=headers, body=b'<p>test</p>')
        self.assertEqual(status, 200)
        status, _ = self.request(path=path, headers={**headers, 'Content-Length': str(server.MAX_FILE_BYTES + 1)}, body=b'')
        self.assertEqual(status, 413)

    def test_overloaded_connections_receive_503(self):
        entered = threading.Event()
        count = []
        original = self.handler.setup
        def setup(handler):
            original(handler)
            count.append(1)
            if len(count) == 2:
                entered.set()
        sockets = []
        try:
            with patch.object(self.handler, 'setup', setup):
                sockets = [socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3)
                           for _ in range(2)]
                self.assertTrue(entered.wait(3))
                extra = socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3)
                try:
                    self.assertIn(b' 503 ', extra.recv(4096).split(b'\r\n')[0])
                finally:
                    extra.close()
        finally:
            for connection in sockets:
                connection.close()

    def test_cli_rejects_public_bind_before_database_or_env_access(self):
        with patch('sys.argv', ['server.py', '--host', '0.0.0.0']), patch('server.load_env_file') as env:
            with self.assertRaises(SystemExit) as caught:
                server.main()
            self.assertEqual(caught.exception.code, 2)
            env.assert_not_called()


if __name__ == '__main__':
    unittest.main()
