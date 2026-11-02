import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from plan_usage import Plans, claude_credentials, normalize_claude, normalize_codex, normalize_grok, rpc_read


class PlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def test_codex_quota_windows_and_credit_units(self):
        value = normalize_codex({'rateLimits': {'planType': 'team', 'primary': {
            'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': 123},
            'secondary': {'usedPercent': 100, 'windowDurationMins': 10080}, 'credits': {'balance': '42.25'}}})
        self.assertEqual(value['plan'], 'team')
        self.assertEqual([w['used_percent'] for w in value['windows']], [0, 100])
        self.assertEqual(value['credits'][0]['value'], 42.25)
        self.assertEqual(value['credits'][0]['unit'], 'credits')
        self.assertFalse(normalize_codex({'rateLimits': {'primary': {}}})['windows'])

    def test_codex_models_and_unlimited_credits_remain_distinct(self):
        value = normalize_codex({'rateLimitsByLimitId': {
            'codex': {'planType': 'pro', 'credits': {'unlimited': True}},
            'model-specific': {'limitName': 'Model', 'primary': {'usedPercent': 50, 'windowDurationMins': 300}}}})
        self.assertTrue(value['credits'][0]['unlimited'])
        self.assertEqual(value['windows'][0]['label'], 'Model · 5 小时')

    def test_claude_subscriptions_and_extra_usage_cents(self):
        value = normalize_claude({'five_hour': {'utilization': 25, 'resets_at': '2026-11-03T00:00:00Z'},
            'seven_day_opus': {'utilization': 50}, 'seven_day_sonnet': None,
            'extra_usage': {'is_enabled': True, 'monthly_limit': 2500, 'used_credits': 500}})
        self.assertEqual(len(value['windows']), 2)
        self.assertEqual(value['credits'][0]['value'], 20)
        self.assertEqual(value['credits'][0]['unit'], 'USD')

    def test_grok_published_usage_and_prepaid_wallet(self):
        value = normalize_grok({'subscription_tier': 'SuperGrok', 'config': {'creditUsagePercent': 18,
            'currentPeriod': {'end': '2026-11-03T00:00:00Z'}, 'prepaidBalance': {'val': '1200'}}})
        self.assertEqual(value['windows'][0]['used_percent'], 18)
        self.assertEqual(value['credits'][0]['value'], 12)
        self.assertEqual(value['plan'], 'SuperGrok')
        missing = normalize_grok({'config': {'currentPeriod': {'end': '2026-11-03'}}})
        self.assertEqual(missing['windows'], [])
        self.assertEqual(missing['credits'], [])
        self.assertEqual(normalize_grok({'config': {'prepaidBalance': {}}})['credits'][0]['value'], 0)

    def test_plan_cache_is_scoped_to_login_and_errors_hide_tokens(self):
        path = self.root / '.credentials.json'
        path.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'first-secret', 'subscriptionType': 'pro'}}))
        oauth = mock.Mock(return_value={'five_hour': {'utilization': 25}})
        plans = Plans(oauth=oauth)
        result = plans.read('claude', self.root)
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn('first-secret', json.dumps(result))
        plans.read('claude', self.root)
        self.assertEqual(oauth.call_count, 1)
        path.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'new-secret'}}))
        oauth.side_effect = OSError('new-secret')
        result = plans.read('claude', self.root)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('new-secret', json.dumps(result))
        self.assertNotIn('windows', result)
        self.assertEqual(oauth.call_count, 2)

    def test_claude_429_sets_retry_window(self):
        (self.root / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fixture'}}))
        oauth = mock.Mock(side_effect=urllib.error.HTTPError('https://api.anthropic.com', 429, 'limited', {'Retry-After': '600'}, None))
        plans = Plans(oauth=oauth)
        result = plans.read('claude', self.root)
        self.assertEqual(result['retry_seconds'], 600)
        plans.read('claude', self.root)
        self.assertEqual(oauth.call_count, 1)

    def test_cli_requests_are_only_initialization_and_read_only_quota(self):
        class Buffer(io.StringIO):
            def close(self):
                pass
        for provider, method in [('codex', 'account/rateLimits/read'), ('grok', '_x.ai/billing')]:
            process = mock.Mock()
            process.stdin = Buffer()
            process.stdout = io.StringIO('\n'.join(json.dumps(item) for item in [
                {'id': 1, 'result': {}}, {'id': 999, 'error': {'message': 'unrelated notification'}},
                {'id': 2, 'result': {'quota': 'fixture'}}]) + '\n')
            with mock.patch('plan_usage.subprocess.Popen', return_value=process):
                self.assertEqual(rpc_read('fixture-cli', self.root, provider), {'quota': 'fixture'})
            sent = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual([item['method'] for item in sent if 'id' in item], ['initialize', method])

    def test_api_key_only_account_does_not_query_official_subscription(self):
        (self.root / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': 'fixture-key'}))
        rpc = mock.Mock()
        self.assertEqual(Plans(rpc=rpc).read('codex', self.root, 'fixture-cli')['status'], 'unavailable')
        rpc.assert_not_called()

    def test_codex_cli_can_own_credentials_outside_auth_file(self):
        rpc = mock.Mock(return_value={'rateLimits': {'primary': {'usedPercent': 10}}})
        self.assertEqual(Plans(rpc=rpc).read('codex', self.root, 'fixture-cli')['status'], 'ok')
        rpc.assert_called_once()

    def test_mac_keychain_read_disables_prompts_and_restores_setting(self):
        import ctypes
        payload = ctypes.create_string_buffer(json.dumps({'claudeAiOauth': {'accessToken': 'keychain-fixture'}}).encode())
        library = mock.Mock()
        def interaction(pointer):
            pointer._obj.value = True
            return 0
        def find(_chain, _length, service, _account_length, _account, length, data, _item):
            self.assertEqual(service, b'Claude Code-credentials')
            length._obj.value = len(payload.value)
            data._obj.value = ctypes.addressof(payload)
            return 0
        library.SecKeychainGetUserInteractionAllowed.side_effect = interaction
        library.SecKeychainSetUserInteractionAllowed.return_value = 0
        library.SecKeychainFindGenericPassword.side_effect = find
        with mock.patch('plan_usage.sys.platform', 'darwin'), mock.patch('plan_usage.pathlib.Path.home', return_value=self.root), mock.patch('ctypes.CDLL', return_value=library):
            self.assertEqual(claude_credentials(self.root / '.claude')['claudeAiOauth']['accessToken'], 'keychain-fixture')
            self.assertEqual(library.SecKeychainSetUserInteractionAllowed.call_args_list[0], mock.call(False))
            self.assertTrue(library.SecKeychainSetUserInteractionAllowed.call_args_list[-1].args[0].value)
            library.SecKeychainItemFreeContent.assert_called_once()
            library.reset_mock()
            self.assertEqual(claude_credentials(self.root / 'custom'), {})
            library.SecKeychainFindGenericPassword.assert_not_called()


if __name__ == '__main__':
    unittest.main()
