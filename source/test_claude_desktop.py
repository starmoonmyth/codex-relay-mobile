import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock

from backend import Chat, DeliveryError
from claude_desktop import deliver_desktop, desktop_target


class DesktopDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.metadata = self.root / 'claude-code-sessions/org/account/local_view.json'
        self.metadata.parent.mkdir(parents=True)
        self.view = {'sessionId': 'local_00000000-0000-4000-8000-000000000001',
                     'cliSessionId': 'cli', 'lastAssistantUuid': 'head'}
        self.metadata.write_text(json.dumps(self.view), 'utf-8')
        self.chat = Chat('claude:cli', 'fixture', str(self.root), str(self.root / 'cli.jsonl'), 1,
                         provider='claude', source='claude-desktop', history_head='head')
        self.runner = Mock(return_value={'ok': True, 'model': 'Opus fixture'})

    def send(self, identity='native-request-0001', text='task'):
        return deliver_desktop(self.chat, text, [], identity, self.root / 'runs',
                               roots=[self.root], runner=self.runner)

    def test_targets_native_view_and_submits_once_per_message_id(self):
        first = self.send()
        self.assertEqual(self.send(), first)
        self.runner.assert_called_once()
        payload = self.runner.call_args.args[0]
        self.assertEqual(payload['native_id'], self.view['sessionId'])
        self.assertEqual(payload['expected_cli'], 'cli')
        self.assertEqual(payload['expected_head'], 'head')
        self.assertEqual(payload['text'], 'task')
        self.assertEqual(first['delivery_mode'], 'claude-desktop')

    def test_unknown_outcome_is_not_replayed(self):
        self.runner.side_effect = DeliveryError('lost response', uncertain=True)
        with self.assertRaises(DeliveryError):
            self.send()
        with self.assertRaisesRegex(DeliveryError, '已经尝试'):
            self.send()
        self.runner.assert_called_once()

    def test_claim_rejects_reusing_id_for_different_message(self):
        self.send()
        with self.assertRaisesRegex(ValueError, '其他内容'):
            self.send(text='other task')
        self.runner.assert_called_once()

    def test_multiple_views_require_native_view_identity(self):
        other = dict(self.view, sessionId='local_00000000-0000-4000-8000-000000000002')
        self.metadata.with_name('local_other.json').write_text(json.dumps(other), 'utf-8')
        with self.assertRaisesRegex(ValueError, '唯一确认'):
            desktop_target(self.chat, [self.root])
        target = desktop_target(replace(self.chat, id='claude-desktop:' + other['sessionId']), [self.root])
        self.assertEqual(target['native_id'], other['sessionId'])

    def test_identity_mismatch_never_runs_desktop_helper(self):
        self.chat = replace(self.chat, rollout_path=str(self.root / 'wrong.jsonl'))
        with self.assertRaisesRegex(ValueError, '记录身份'):
            self.send()
        self.runner.assert_not_called()

    def test_image_only_and_attachments_do_not_modify_native_draft(self):
        for text, images in [('', []), ('task', [self.root / 'image.png'])]:
            with self.assertRaises(ValueError):
                deliver_desktop(self.chat, text, images, 'native-request-0001', self.root / 'runs',
                                roots=[self.root], runner=self.runner)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
