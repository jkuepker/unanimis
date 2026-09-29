import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from unanimis import answer_check
from unanimis.core import Core
from unanimis.mcp import Server, TOOLS

ENV = {'UNANIMIS_ANSWER_CHECK_URL': 'http://127.0.0.1:9'}


def fake_post(url, state, instructions, timeout):
    """A stand-in decision model: the record answers when it names the product."""
    return 0.9 if 'unanimis' in state else 0.1


class AnswerCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core(self.temp.name)
        self.named = self.core.store('We named the product unanimis.', 'name', title='Naming decision')
        self.other = self.core.store('The product logo has two halves.', 'logo', title='Logo notes')

    def tearDown(self):
        self.core.close()
        self.temp.cleanup()

    def recall(self, query, env=ENV, post=fake_post, **kw):
        with patch.dict(os.environ, env, clear=False), patch('unanimis.answer_check.ask', post):
            return self.core.recall(query, **kw)

    def test_off_unless_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('UNANIMIS_ANSWER_CHECK_URL', None)
            result = self.core.recall('product')
        self.assertNotIn('answer_check', result)
        self.assertIsNone(answer_check.settings({}))

    def test_answer_likely_names_best_record(self):
        result = self.recall('product')
        check = result['answer_check']
        self.assertEqual(check['status'], 'answer_likely')
        self.assertEqual(check['best_record_id'], self.named['record_id'])
        self.assertEqual(check['probability'], 0.9)
        self.assertEqual(len(check['checked']), len(result['matches']))
        self.assertIn('limitation', check)

    def test_answer_unlikely_below_threshold(self):
        check = self.recall('product', post=lambda *a: 0.2)['answer_check']
        self.assertEqual(check['status'], 'answer_unlikely')
        check = self.recall('product', env=dict(ENV, UNANIMIS_ANSWER_CHECK_THRESHOLD='0.1'), post=lambda *a: 0.2)['answer_check']
        self.assertEqual(check['status'], 'answer_likely')

    def test_top_limits_checked_records(self):
        check = self.recall('product', env=dict(ENV, UNANIMIS_ANSWER_CHECK_TOP='1'))['answer_check']
        self.assertEqual(len(check['checked']), 1)

    def test_failure_is_reported_and_recall_still_returns_matches(self):
        def down(*args):
            raise ConnectionRefusedError('connection refused')
        result = self.recall('product', post=down)
        self.assertEqual(result['answer_check']['status'], 'unavailable')
        self.assertIn('ConnectionRefusedError', result['answer_check']['reason'])
        self.assertEqual(len(result['matches']), 2)

    def test_time_budget_is_enforced(self):
        check = self.recall('product', env=dict(ENV, UNANIMIS_ANSWER_CHECK_TIMEOUT='0'))['answer_check']
        self.assertEqual(check['status'], 'unavailable')
        self.assertIn('TimeoutError', check['reason'])

    def test_non_loopback_url_is_refused_without_sending(self):
        sent = []
        check = self.recall('product', env={'UNANIMIS_ANSWER_CHECK_URL': 'http://example.com:8019'},
                            post=lambda *a: sent.append(a) or 0.9)['answer_check']
        self.assertEqual(check['status'], 'unavailable')
        self.assertIn('loopback', check['reason'])
        self.assertEqual(sent, [])

    def test_only_the_first_page_and_nonempty_results_are_checked(self):
        self.assertNotIn('answer_check', self.recall('product', offset=1))
        self.assertEqual(self.recall('zebra')['answer_check']['status'], 'not_checked')

    def test_http_round_trip_against_a_systemone_endpoint(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, body))
                p = 0.8 if 'unanimis' in body['state'] else 0.05
                payload = json.dumps({'model': 'kev-latest', 'answers': {'answer': {'type': 'noul', 'noul': p}}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            # A configured proxy must not receive record text.
            env = {'UNANIMIS_ANSWER_CHECK_URL': 'http://127.0.0.1:%s/' % httpd.server_address[1],
                   'http_proxy': 'http://127.0.0.1:9', 'HTTP_PROXY': 'http://127.0.0.1:9', 'no_proxy': '', 'NO_PROXY': ''}
            with patch.dict(os.environ, env, clear=False):
                check = self.core.recall('what did we name the product')['answer_check']
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(check['status'], 'answer_likely')
        self.assertEqual(check['probability'], 0.8)
        path, body = seen[0]
        self.assertEqual(path, '/v1/systemone')
        question = body['questions']['answer']
        self.assertEqual(question['type'], 'noul')
        self.assertIn('what did we name the product', question['instructions'])
        self.assertTrue(body['state'].startswith(('Naming decision\n', 'Logo notes\n')))

    def test_out_of_range_probability_is_rejected(self):
        with patch('unanimis.answer_check._OPENER.open') as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b'{"answers": {"answer": {"noul": 1.5}}}'
            with self.assertRaises(ValueError):
                answer_check.ask('http://127.0.0.1:9', 'state', 'question', 1)

    def test_mcp_recall_carries_answer_check_and_describes_it(self):
        server = Server(self.core)
        server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}})
        server.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        with patch.dict(os.environ, ENV, clear=False), patch('unanimis.answer_check.ask', fake_post):
            response = server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                        'params': {'name': 'unim_recall', 'arguments': {'query': 'product'}}})
        self.assertFalse(response['result']['isError'])
        self.assertEqual(response['result']['structuredContent']['answer_check']['status'], 'answer_likely')
        recall_tool = next(t for t in TOOLS if t['name'] == 'unim_recall')
        self.assertIn('answer_check', recall_tool['description'])


if __name__ == '__main__':
    unittest.main()
