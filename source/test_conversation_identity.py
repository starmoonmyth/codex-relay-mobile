import base64
import dataclasses
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from activity import transcript_activity
from claude_backend import ClaudeData, deliver_claude
from desktop_records import GrokBotRecords
from grok_backend import GrokData, deliver_grok
from server import Relay


class ConversationIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.home = self.root / 'claude'
        self.path = self.home / 'projects/project/session.jsonl'
        self.path.parent.mkdir(parents=True)
        self.desktop = self.root / 'desktop'
        self.metadata = self.desktop / 'claude-code-sessions/org/account/local_desktop.json'
        self.metadata.parent.mkdir(parents=True)
        self.entries = [self.message('root', None, 'user', 'shared'),
                        self.message('desk', 'root', 'assistant', 'desktop answer'),
                        self.message('phone', 'root', 'user', 'phone task'),
                        self.message('phone-answer', 'phone', 'assistant', 'phone answer')]
        self.write()

    def message(self, identity, parent, role, text):
        return {'type': role, 'uuid': identity, 'parentUuid': parent, 'sessionId': 'session',
                'cwd': str(self.root), 'message': {'id': 'api-' + identity, 'content': text,
                'model': 'fixture', 'stop_reason': 'end_turn'}}

    def write(self):
        self.path.write_text('\n'.join(json.dumps(entry) for entry in self.entries) + '\n', 'utf-8')

    def desktop_view(self, head='desk'):
        self.metadata.write_text(json.dumps({'sessionId': 'local_desktop', 'cliSessionId': 'session',
            'lastAssistantUuid': head, 'title': 'Desktop fixture', 'cwd': str(self.root)}), 'utf-8')

    def test_cli_history_follows_one_parent_chain_and_ignores_foreign_sessions(self):
        self.entries.append(dict(self.message('foreign', 'desk', 'assistant', 'wrong session'), sessionId='other'))
        self.write()
        data = ClaudeData(self.home)
        chat = data.chats()[0]
        self.assertEqual(chat.source, 'claude-cli')
        self.assertEqual(chat.history_head, 'phone-answer')
        self.assertEqual([m.text for m in data.history(chat)], ['shared', 'phone task', 'phone answer'])
        self.assertEqual(data.history(chat)[-1].id, 'phone-answer')

    def test_desktop_history_and_activity_follow_selected_head_without_cli_sibling(self):
        self.desktop_view()
        data = ClaudeData(self.home, [self.desktop])
        chat = data.chats()[0]
        self.assertFalse(chat.writable)
        self.assertEqual(chat.source, 'claude-desktop')
        self.assertEqual([m.text for m in data.history(chat)], ['shared', 'desktop answer'])
        self.assertEqual(transcript_activity(chat)['message_id'], 'desk')
        self.desktop_view('phone-answer')
        self.assertEqual([m.text for m in data.history(data.chats()[0])], ['shared', 'phone task', 'phone answer'])

    def test_desktop_latest_last_prompt_overrides_stale_assistant_metadata(self):
        self.desktop_view('desk')
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'type': 'last-prompt', 'leafUuid': 'phone-answer', 'sessionId': 'session'}) + '\n')
        data = ClaudeData(self.home, [self.desktop])
        self.assertEqual(data.chats()[0].history_head, 'phone-answer')

    def test_interrupted_tool_use_is_not_reported_as_working(self):
        self.desktop_view('desk')
        self.entries.extend([
            {'type': 'user', 'uuid': 'rejected', 'parentUuid': 'desk', 'sessionId': 'session',
             'toolUseResult': 'User rejected tool use', 'message': {'role': 'user', 'content': []}},
            {'type': 'user', 'uuid': 'interrupted', 'parentUuid': 'rejected', 'sessionId': 'session',
             'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}]}},
        ])
        self.write()
        data = ClaudeData(self.home, [self.desktop])
        chat = dataclasses.replace(data.chats()[0], history_head='interrupted')
        self.assertEqual(transcript_activity(chat)['state'], 'interrupted')

    def test_missing_desktop_head_never_falls_back_to_an_unrelated_leaf(self):
        data = ClaudeData(self.home, [self.desktop])
        for head in ('missing', ''):
            self.desktop_view(head)
            self.assertEqual(data.history(data.chats()[0]), [])

    def test_desktop_session_cannot_be_sent_to_cli_even_through_api(self):
        self.desktop_view()
        relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'claude',
            'claude_home': str(self.home), 'codex_home': str(self.root / 'codex')})
        relay.claude = ClaudeData(self.home, [self.desktop])
        with patch.object(relay, 'claude_sender') as sender:
            with self.assertRaisesRegex(ValueError, 'Claude 桌面'):
                relay.send('claude:session', 'new task', 'blocked-desktop-request')
            sender.assert_not_called()
        with relay.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0], 0)

    def test_off_branch_receipt_is_detached_and_does_not_change_desktop_status(self):
        self.desktop_view()
        relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'claude',
            'claude_home': str(self.home), 'codex_home': str(self.root / 'codex')})
        relay.claude = ClaudeData(self.home, [self.desktop])
        receipt = {'id': 'old-request', 'thread_id': 'claude:session', 'text': 'phone task',
            'status': '处理中', 'result': 'sent', 'message_id': 'phone', 'queue_ids': [], 'provider': 'claude'}
        with relay.connect() as connection:
            connection.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',
                (receipt['id'], receipt['thread_id'], receipt['text'], json.dumps(receipt), 1))
        snapshot = relay.snapshot('claude:session')
        self.assertTrue(snapshot['receipts'][0]['detached'])
        self.assertFalse(snapshot['receipts'][0]['seen_in_history'])
        self.assertEqual(snapshot['activity']['state'], 'completed')
        self.assertEqual([m['text'] for m in snapshot['messages']], ['shared', 'desktop answer'])

    def test_cli_delivery_pins_config_home_and_head_and_rejects_stale_branch(self):
        cli = self.root / 'claude.exe'
        cli.touch()
        data = ClaudeData(self.home)
        chat = data.chats()[0]
        with patch('claude_backend.subprocess.Popen') as launch:
            deliver_claude(str(cli), chat, 'task', [], 'request-id', model='fixture', home=self.home, run_dir=self.root / 'runs')
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index('--resume-session-at') + 1], 'phone-answer')
            self.assertEqual(launch.call_args.kwargs['env']['CLAUDE_CONFIG_DIR'], str(self.home))
        self.entries.append(self.message('later', 'phone-answer', 'user', 'later task'))
        self.write()
        with patch('claude_backend.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, '已变化'):
                deliver_claude(str(cli), chat, 'task', [], 'request-id', model='fixture', home=self.home)
            launch.assert_not_called()

    def test_source_binding_rejects_stale_phone_target(self):
        relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'claude',
            'claude_home': str(self.home), 'codex_home': str(self.root / 'codex')})
        with patch.object(relay, 'claude_sender') as sender:
            with self.assertRaisesRegex(ValueError, '来源已变化'):
                relay.send('claude:session', 'task', 'stale-record-request', record_key='wrong')
            with self.assertRaisesRegex(ValueError, '消息链已更新'):
                relay.send('claude:session', 'task', 'stale-record-request', history_head='desk')
            sender.assert_not_called()

    def test_grok_bot_and_cli_with_same_title_remain_distinct_records(self):
        from test_grok_backend import make_session
        home = self.root / 'grok'
        directory = make_session(home, self.root)
        bot_root = self.root / 'bot'
        bot_root.mkdir()
        def blob(key, value):
            name = base64.b32encode(key.encode()).decode().lower().rstrip('=') + '.blob'
            (bot_root / name).write_text(json.dumps({'schemaVersion': 1, 'value': value}), 'utf-8')
        identity = directory.name
        blob('sand.client.slice.account.a.roster.last-roster', {'rows': [{'id': identity, 'title': 'Grok fixture'}]})
        blob('sand.client.slice.account.a.transcript.replicas.' + identity, {'entries': [
            {'kind': 'send-message', 'id': 'u', 'message': {'type': 'text', 'content': 'native task'}},
            {'kind': 'message', 'id': 'a', 'role': 'assistant', 'content': 'native answer'}]})
        data = GrokData(home, bot_root)
        chats = data.chats()
        self.assertEqual(len(chats), 2)
        self.assertEqual(len({chat.id for chat in chats}), 2)
        native = next(chat for chat in chats if chat.source == 'grok-bot')
        self.assertEqual([m.text for m in data.history(native)], ['native task', 'native answer'])
        with patch('grok_backend.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'Grok Bot'):
                deliver_grok('fixture', home, native, 'task', 'native-request-1234', self.root / 'runs', 'fixture')
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
