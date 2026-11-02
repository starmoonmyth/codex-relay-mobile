import contextlib
import dataclasses
import http.server
import json
import pathlib
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from backend import Chat
from usage import Usage, balance_credentials, fetch_balance, local_usage, normalize_balance, usage_url


class UsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.path = self.root / 'chat.jsonl'
        self.chat = Chat('fixture', 'Fixture', str(self.root), str(self.path), 0)

    def write(self, entries):
        self.path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', 'utf-8')

    def test_codex_uses_latest_cumulative_snapshot_not_sum(self):
        def count(total, last, **extra):
            return {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': total, 'cached_input_tokens': 80, 'output_tokens': 10, 'reasoning_output_tokens': 4},
                'last_token_usage': {'input_tokens': last, 'output_tokens': 5}, 'model_context_window': 200000}, **extra}}
        self.write([count(100, 100), count(300, 200, rate_limits={'primary': {
            'used_percent': 100, 'window_minutes': 300, 'resets_at': 123}}),
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None, 'rate_limits': None}}])
        with self.path.open('a') as output:
            output.write('{partial')
        result = local_usage(self.chat)
        self.assertEqual(result['total']['total_tokens'], 310)
        self.assertEqual(result['total']['input_tokens'], 300)
        self.assertEqual(result['last']['total_tokens'], 205)
        self.assertEqual(result['context_window'], 200000)
        self.assertEqual(result['windows'][0]['used_percent'], 100)

    def test_claude_deduplicates_message_usage_and_counts_cache_in_input(self):
        def assistant(identity, output, **extra):
            return {'type': 'assistant', 'message': {'id': identity, 'usage': {
                'input_tokens': 10, 'cache_read_input_tokens': 70, 'cache_creation_input_tokens': 20,
                'output_tokens': output, 'output_tokens_details': {'thinking_tokens': 3}}}, **extra}
        partial = assistant('a', 12)
        partial['message']['usage'].update(input_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        self.write([assistant('a', 5), partial, assistant('b', 8), assistant('side', 1000, isSidechain=True)])
        result = local_usage(dataclasses.replace(self.chat, provider='claude'))
        self.assertEqual(result['total']['input_tokens'], 200)
        self.assertEqual(result['total']['output_tokens'], 20)
        self.assertEqual(result['total']['total_tokens'], 220)
        self.assertEqual(result['last']['total_tokens'], 108)
        self.assertEqual(result['total']['cached_input_tokens'], 140)

    def test_grok_completed_prompt_usage_is_counted_once(self):
        def completion(identity, amount):
            return {'params': {'update': {'sessionUpdate': 'turn_completed', 'prompt_id': identity,
                'usage': {'inputTokens': amount, 'cachedReadTokens': 40, 'outputTokens': 10, 'reasoningTokens': 3}}}}
        self.write([completion('one', 100), completion('one', 100), completion('two', 200)])
        result = local_usage(dataclasses.replace(self.chat, provider='grok'))
        self.assertEqual(result['total']['total_tokens'], 320)
        self.assertEqual(result['last']['total_tokens'], 210)

    def test_missing_usage_is_unknown_not_zero(self):
        result = local_usage(self.chat)
        self.assertFalse(result['available'])
        self.assertIsNone(result['total'])
        self.write([{'type': 'assistant', 'message': {'usage': {}}}])
        self.assertFalse(local_usage(dataclasses.replace(self.chat, provider='claude'))['available'])

    def test_balance_zero_unlimited_and_absent_fields(self):
        self.assertEqual(normalize_balance({'remaining': 0, 'unit': 'USD'})['remaining'], 0)
        self.assertEqual(normalize_balance({'remaining': -1, 'unit': 'USD'})['remaining'], -1)
        with self.assertRaises(ValueError):
            normalize_balance({})
        with self.assertRaises(ValueError):
            normalize_balance({'isValid': False, 'remaining': 10})

    def test_balance_quota_subscription_and_actual_deduction(self):
        result = normalize_balance({'quota': {'remaining': 5, 'unit': 'USD'},
            'usage': {'today': {'cost': 100, 'actual_cost': 2.5, 'requests': 3}},
            'subscription': {'daily_limit_usd': 20, 'daily_usage_usd': 15}, 'api_key': 'never-return-this'})
        self.assertEqual(result['remaining'], 5)
        self.assertEqual(result['today']['cost'], 2.5)
        self.assertEqual(result['limits'][0]['remaining'], 5)
        self.assertNotIn('never-return-this', json.dumps(result))
        self.assertIsNone(normalize_balance({'remaining': 3, 'usage': {'today': {'cost': 100}}})['today']['cost'])

    def test_query_uses_configured_origin_and_https(self):
        self.assertEqual(usage_url('https://fixture.invalid/prefix/v1/'), 'https://fixture.invalid/prefix/v1/usage')
        for url in ['http://remote.invalid', 'https://user:password@fixture.invalid', 'https://fixture.invalid?key=secret']:
            with self.assertRaises(ValueError):
                usage_url(url)

    def write_codex_config(self, base='https://fixture.invalid/v1'):
        (self.root / 'config.toml').write_text(f'model_provider="custom"\n[model_providers.custom]\nbase_url="{base}"\n')

    def test_native_api_key_and_oauth_separation(self):
        self.write_codex_config()
        (self.root / 'auth.json').write_text(json.dumps({'tokens': {'access_token': 'oauth-secret'}}))
        self.assertIsNone(balance_credentials('codex', self.root, self.root / 'missing-switch'))
        (self.root / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'api-secret'}))
        url, key, source = balance_credentials('codex', self.root)
        self.assertEqual(url, 'https://fixture.invalid/v1/usage')
        self.assertEqual(key, 'api-secret')

    def test_switch_credentials_require_current_matching_provider(self):
        self.write_codex_config()
        database = self.root / 'cc-switch.db'
        settings = {'config': (self.root / 'config.toml').read_text(), 'auth': {'OPENAI_API_KEY': 'switch-secret'}}
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE providers (app_type TEXT,is_current INT,settings_config TEXT)')
            connection.execute('INSERT INTO providers VALUES (?,?,?)', ('codex', 1, json.dumps(settings)))
        credentials = balance_credentials('codex', self.root, self.root)
        self.assertEqual(credentials[1], 'switch-secret')
        self.assertIn('可能不同', credentials[2])
        self.write_codex_config('https://other.invalid')
        self.assertIsNone(balance_credentials('codex', self.root, self.root))

    def test_balance_cache_does_not_mix_credentials(self):
        fetcher = mock.Mock(return_value={'remaining': 10, 'unit': 'USD'})
        usage = Usage(fetcher)
        with mock.patch('usage.balance_credentials', return_value=('https://fixture.invalid/v1/usage', 'first-key', 'fixture')):
            self.assertEqual(usage.balance('codex', self.root)['remaining'], 10)
            usage.balance('codex', self.root)
        self.assertEqual(fetcher.call_count, 1)
        with mock.patch('usage.balance_credentials', return_value=('https://fixture.invalid/v1/usage', 'second-key', 'fixture')):
            usage.balance('codex', self.root)
        self.assertEqual(fetcher.call_count, 2)

    def test_failed_query_does_not_leak_credentials_or_fabricate_balance(self):
        usage = Usage(mock.Mock(side_effect=OSError('secret-token')))
        with mock.patch('usage.balance_credentials', return_value=('https://fixture.invalid/v1/usage', 'secret-token', 'fixture')):
            result = usage.balance('codex', self.root)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('remaining', result)
        self.assertNotIn('secret-token', json.dumps(result))

    def test_redirect_is_not_followed_with_api_key(self):
        paths = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/sink')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError):
                fetch_balance(f'http://127.0.0.1:{server.server_address[1]}/usage', 'secret')
            self.assertEqual(paths, ['/usage'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
