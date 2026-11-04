import json
import pathlib
import tempfile
import unittest
from unittest import mock

import requests

from updates import CHECK_INTERVAL, RELEASES_URL, Updates, version_key


def release(tag):
    return dict(tag_name=tag, draft=False, prerelease='-' in tag)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        patcher = mock.patch('updates.requests.get')
        self.network = patcher.start()
        self.addCleanup(patcher.stop)
        self.network.return_value.status_code = 200

    def test_stable_channel_queries_latest_and_never_offers_preview(self):
        checker = Updates(self.root, current='1.3.7', baseline='1.3.7')
        self.network.return_value.json.return_value = release('v1.3.8-rc.2')
        result = checker.check()
        self.assertFalse(result['available'])
        self.assertTrue(result['error'])
        self.assertTrue(self.network.call_args.args[0].endswith('/releases/latest'))
        self.assertFalse(self.network.call_args.kwargs['allow_redirects'])
        self.assertNotIn('Authorization', self.network.call_args.kwargs['headers'])
        self.network.return_value.json.return_value = release('v1.3.8')
        with mock.patch('updates.time.time', return_value=result['checked_at'] + 61):
            result = checker.check(force=True)
        self.assertTrue(result['available'])
        self.assertEqual(result['release_url'], RELEASES_URL + '/tag/v1.3.8')

    def test_preview_uses_semantic_order_and_public_baseline_for_local_builds(self):
        checker = Updates(self.root, current='1.3.16', baseline='1.3.8-rc.2')
        self.network.return_value.json.return_value = [release('v1.3.8-rc.3'), release('v1.3.8-rc.10'),
            release('v1.3.8-rc.9'), dict(release('v9.0.0'), draft=True), release('https://example.com')]
        result = checker.check()
        self.assertEqual(result['latest_version'], 'v1.3.8-rc.10')
        self.assertTrue(result['available'])
        self.assertIn('/releases?per_page=30', self.network.call_args.args[0])
        self.assertGreater(version_key('v1.3.8'), version_key('v1.3.8-rc.99'))
        self.assertEqual(version_key('v1.3.8+build.1'), version_key('1.3.8'))

    def test_daily_cache_survives_restart_and_manual_check_has_shared_cooldown(self):
        self.network.return_value.json.return_value = [release('v1.3.8-rc.2')]
        checker = Updates(self.root, current='1.3.16', baseline='1.3.8-rc.2')
        first = checker.check()
        self.assertFalse(first['available'])
        checker.check(force=True)
        self.assertEqual(Updates(self.root, current='1.3.16', baseline='1.3.8-rc.2').check(), first)
        self.assertEqual(self.network.call_count, 1)
        with mock.patch('updates.time.time', return_value=first['checked_at'] + 61):
            checker.check(force=True)
        self.assertEqual(self.network.call_count, 2)
        with mock.patch('updates.time.time', return_value=first['checked_at'] + CHECK_INTERVAL + 62):
            checker.check()
        self.assertEqual(self.network.call_count, 3)

    def test_network_failure_preserves_known_update_and_never_claims_latest(self):
        checker = Updates(self.root, current='1.3.16', baseline='1.3.8-rc.2')
        self.network.return_value.json.return_value = [release('v1.3.8-rc.3')]
        first = checker.check()
        self.network.side_effect = requests.ConnectionError('private proxy credentials')
        with mock.patch('updates.time.time', return_value=first['checked_at'] + 61):
            failed = checker.check(force=True)
        self.assertTrue(failed['available'])
        self.assertTrue(failed['error'])
        self.assertNotIn('private', json.dumps(failed))
        fresh = Updates(self.root / 'missing')
        self.assertFalse(fresh.check()['available'])
        self.assertIsNone(fresh.check()['latest_version'])

    def test_published_local_build_does_not_alert_until_a_later_release(self):
        checker = Updates(self.root, current='1.3.17', baseline='1.3.8-rc.3')
        self.network.return_value.json.return_value = [release('v1.3.8-rc.3')]
        result = checker.check()
        self.assertFalse(result['available'])
        self.assertFalse(Updates(self.root, current='1.3.17', baseline='1.3.8-rc.3').check()['available'])
        self.network.return_value.json.return_value = [release('v1.3.8-rc.4')]
        with mock.patch('updates.time.time', return_value=result['checked_at'] + 61):
            self.assertTrue(checker.check(force=True)['available'])

    def test_corrupt_cache_and_invalid_release_data_can_be_rechecked(self):
        (self.root / 'update-check.json').write_text('[]')
        checker = Updates(self.root, current='1.3.16', baseline='1.3.8-rc.2')
        self.network.return_value.json.return_value = {'message': 'Not Found'}
        result = checker.check()
        self.assertTrue(result['error'])
        self.assertEqual(result['release_url'], RELEASES_URL)
        self.assertIsNone(result['latest_version'])

    def test_stable_public_baseline_keeps_equivalent_local_build_current(self):
        checker = Updates(self.root, current='1.3.19', baseline='1.3.8')
        self.network.return_value.json.return_value = release('v1.3.8')
        result = checker.check()
        self.assertEqual(result['channel'], 'stable')
        self.assertFalse(result['available'])
        self.assertFalse(Updates(self.root, current='1.3.19', baseline='1.3.8').check()['available'])
        self.network.return_value.json.return_value = release('v1.3.9')
        with mock.patch('updates.time.time', return_value=result['checked_at'] + 61):
            self.assertTrue(checker.check(force=True)['available'])


if __name__ == '__main__':
    unittest.main()
