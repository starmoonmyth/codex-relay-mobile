import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from grok_backend import GrokData, deliver_grok, run_cli, run_status
from activity import Activities, transcript_activity
from server import Relay

SESSION = '00000000-0000-4000-8000-000000000001'


def make_session(home, cwd):
    directory = home / 'sessions/project' / SESSION
    directory.mkdir(parents=True)
    (directory / 'summary.json').write_text(json.dumps({'info': {'id': SESSION, 'cwd': str(cwd)},
        'generated_title': 'Grok fixture', 'current_model_id': 'grok-fixture'}), 'utf-8')
    (directory / 'updates.jsonl').write_text('', 'utf-8')
    return directory


def update(kind, text='', identity='', **extra):
    return {'timestamp': 123, 'params': {'update': {'sessionUpdate': kind,
            'content': {'type': 'text', 'text': text}, **extra},
            '_meta': {'eventId': identity, 'promptId': 'prompt-fixture', 'streamStartMs': 100}}}


class GrokTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.home = self.root / 'grok'
        self.directory = make_session(self.home, self.root)
        self.data = GrokData(self.home)

    def write_updates(self, *entries):
        (self.directory / 'updates.jsonl').write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', 'utf-8')

    def test_history_chunks_models_and_explicit_completion(self):
        child = self.directory.parent / '00000000-0000-4000-8000-000000000002'
        child.mkdir()
        (child / 'summary.json').write_text(json.dumps({'session_kind': 'subagent', 'info': {'id': child.name, 'cwd': str(self.root)}}))
        self.assertEqual(len(self.data.chats()), 1)
        chat = self.data.chats()[0]
        self.write_updates(update('user_message_chunk', '任务', 'user', _meta={'modelId': 'grok-requested'}),
            update('agent_thought_chunk', 'private thought', 'thought'),
            update('agent_message_chunk', '第一', 'agent-1'), update('agent_message_chunk', '部分', 'agent-2'))
        messages = self.data.history(chat)
        self.assertEqual([(message.role, message.text) for message in messages], [('user', '任务'), ('assistant', '第一部分')])
        self.assertEqual(messages[-1].model, 'grok-requested')
        self.assertEqual(self.data.turn(chat.id)['status'], 'inProgress')
        with (self.directory / 'updates.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps(update('turn_completed', stop_reason='end_turn')) + '\n')
        self.assertEqual(self.data.turn(chat.id)['status'], 'completed')

    def test_long_running_turn_keeps_state_after_user_and_permission_leave_display_window(self):
        old_user = update('user_message_chunk', 'previous task', 'old-user')
        old_user['timestamp'] = 100
        old_end = update('turn_completed', 'old answer', 'old-end', stop_reason='end_turn')
        old_end['timestamp'] = 101
        self.write_updates(old_user, old_end, update('user_message_chunk', 'long task', 'user'),
            update('agent_message_chunk', 'progress report', 'progress'),
            update('tool_call_update', '大' * 800000, 'tool'),
            update('agent_thought_chunk', 'continuing', 'thought'))
        registry = self.home / 'active_sessions.json'
        registry.write_text(json.dumps([{'session_id': SESSION, 'pid': os.getpid()}]))
        events = self.directory / 'events.jsonl'
        events.write_text(json.dumps({'type': 'turn_ended', 'ts': '1970-01-01T00:01:41Z', 'outcome': 'completed'}) + '\n' +
            json.dumps({'type': 'permission_requested', 'ts': '1970-01-01T00:02:04Z'}) + '\n' +
            json.dumps({'type': 'phase_changed', 'ts': '1970-01-01T00:02:05Z', 'padding': 'x' * 300000}) + '\n')
        chat = self.data.chats()[0]
        activities = Activities()
        self.assertEqual(self.data.turn(chat.id)['status'], 'needsInput')
        self.assertEqual(activities.transcript(chat)['state'], 'needs_input')
        with events.open('a') as handle:
            handle.write(json.dumps({'type': 'permission_resolved', 'ts': '1970-01-01T00:02:06Z'}) + '\n')
        self.assertEqual(self.data.turn(chat.id)['status'], 'inProgress')
        self.assertEqual(transcript_activity(chat)['state'], 'working')
        self.assertEqual(transcript_activity(chat)['completion_id'], '')
        with events.open('a') as handle:
            handle.write(json.dumps({'type': 'turn_ended', 'ts': '1970-01-01T00:02:07Z', 'outcome': 'cancelled'}) + '\n')
        self.assertEqual(self.data.turn(chat.id)['status'], 'interrupted')
        self.assertEqual(transcript_activity(chat)['state'], 'interrupted')
        with (self.directory / 'updates.jsonl').open('a') as handle:
            handle.write(json.dumps(update('turn_completed', stop_reason='end_turn')) + '\n')
        with events.open('a') as handle:
            handle.write(json.dumps({'type': 'turn_ended', 'ts': '1970-01-01T00:02:08Z', 'outcome': 'completed'}) + '\n')
        self.assertEqual(self.data.turn(chat.id)['status'], 'completed')
        self.assertEqual(transcript_activity(chat)['state'], 'completed')

    def test_reverse_state_reader_ignores_partial_tail_and_handles_utf8_blocks(self):
        self.write_updates(update('user_message_chunk', 'task', 'user'),
            update('agent_message_chunk', '进度' * 40000, 'progress'))
        with (self.directory / 'updates.jsonl').open('a') as handle:
            handle.write('{"params":')
        chat = self.data.chats()[0]
        self.assertEqual(self.data.turn(chat.id)['status'], 'inProgress')
        self.assertEqual(transcript_activity(chat)['state'], 'working')

    def test_native_terminal_events_and_permissions_follow_current_turn(self):
        self.write_updates(update('user_message_chunk', 'task', 'user'))
        events = self.directory / 'events.jsonl'
        for outcome, expected in [('cancelled', 'interrupted'), ('completed', 'completed'), ('error', 'failed')]:
            with self.subTest(outcome=outcome):
                events.write_text(json.dumps({'type': 'turn_ended', 'ts': '1970-01-01T00:02:04Z', 'outcome': outcome}))
                self.assertEqual(self.data.turn('grok:' + SESSION)['status'], expected)
                self.assertEqual(transcript_activity(self.data.chats()[0])['state'], expected)
        events.write_text(json.dumps({'type': 'permission_requested', 'ts': '1970-01-01T00:02:04Z'}))
        self.assertEqual(self.data.turn('grok:' + SESSION)['status'], 'needsInput')
        self.assertEqual(transcript_activity(self.data.chats()[0])['state'], 'needs_input')
        events.write_text(json.dumps({'type': 'turn_ended', 'ts': '1970-01-01T00:02:02Z', 'outcome': 'cancelled'}))
        self.assertEqual(self.data.turn('grok:' + SESSION)['status'], 'inProgress')

    def test_inactive_session_stops_after_grace_and_cache_refreshes(self):
        self.write_updates(update('user_message_chunk', 'task', 'user'))
        chat = self.data.chats()[0]
        registry = self.home / 'active_sessions.json'
        registry.write_text('[]')
        self.assertEqual(self.data.turn(chat.id)['status'], 'inProgress')
        os.utime(self.directory / 'updates.jsonl', (1, 1))
        registry.write_text(json.dumps([{'session_id': SESSION, 'pid': os.getpid()}]))
        activities = Activities()
        with mock.patch('activity.time.time', return_value=100):
            self.assertEqual(activities.transcript(chat)['state'], 'working')
        registry.write_text('[]')
        with mock.patch('activity.time.time', return_value=108):
            self.assertEqual(activities.transcript(chat)['state'], 'interrupted')
        registry.unlink()
        self.assertEqual(self.data.turn(chat.id)['status'], 'inProgress')

    def test_native_stop_reconciles_mobile_receipt_and_list(self):
        self.write_updates(update('user_message_chunk', 'task', 'user'))
        (self.directory / 'events.jsonl').write_text(json.dumps({
            'type': 'turn_ended', 'ts': '1970-01-01T00:02:04Z', 'outcome': 'cancelled'}))
        relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'grok', 'grok_home': str(self.home),
            'codex_home': str(self.root / 'codex'), 'claude_home': str(self.root / 'claude')})
        chat = relay.grok.chats()[0]
        receipt = {'id': 'mobile-request', 'thread_id': chat.id, 'text': 'task', 'provider': 'grok',
            'result': 'sent', 'status': '处理中', 'queue_ids': [], 'sent_at_ms': 122000}
        with relay.connect() as c:
            c.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',
                (receipt['id'], chat.id, receipt['text'], json.dumps(receipt), 1))
        value = relay.snapshot(chat.id)
        self.assertEqual(value['turn']['status'], 'interrupted')
        self.assertEqual(value['activity']['state'], 'interrupted')
        self.assertEqual(value['receipts'][0]['status'], '已中断')
        self.assertTrue(value['receipts'][0]['seen_in_history'])

    def test_successful_mobile_run_completes_only_its_own_native_turn(self):
        self.write_updates(update('user_message_chunk', 'task', 'user'), update('agent_message_chunk', 'done', 'answer'))
        relay = Relay(self.root / 'relay', {'origin': 'http://localhost:8879', 'provider': 'grok',
            'grok_home': str(self.home), 'codex_home': str(self.root / 'codex'), 'claude_home': str(self.root / 'claude')})
        chat = relay.grok.chats()[0]
        receipt = {'id': 'mobile-request', 'thread_id': chat.id, 'text': 'task', 'provider': 'grok',
            'result': 'sent', 'status': '处理中', 'queue_ids': [], 'sent_at_ms': 122000}
        run = relay.root / 'grok-runs' / receipt['id']
        run.mkdir(parents=True)
        (run / 'result.json').write_text('{"ok":true}')
        with relay.connect() as c:
            c.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',
                (receipt['id'], chat.id, receipt['text'], json.dumps(receipt), 1))
        value = relay.snapshot(chat.id)
        self.assertEqual(value['activity']['state'], 'completed')
        self.assertTrue(value['activity']['visible_completion'])
        newer = update('user_message_chunk', 'later task', 'later-user')
        newer['timestamp'] = 130
        with (self.directory / 'updates.jsonl').open('a') as f:
            f.write(json.dumps(newer) + '\n')
        self.assertEqual(relay.snapshot(chat.id)['activity']['state'], 'working')

    def test_delivery_uses_prompt_file_and_preserves_model(self):
        cli = self.root / 'fixture-cli'
        cli.touch()
        chat = self.data.chats()[0]
        with mock.patch('grok_backend.subprocess.Popen') as popen:
            popen.return_value.pid = 100
            result = deliver_grok(str(cli), self.home, chat, '长任务' * 8000, 'request-fixture-0001', self.root / 'runs', 'grok-exact')
        self.assertEqual(result['delivery_mode'], 'grok-cli')
        descriptor = self.root / 'runs/request-fixture-0001/request.json'
        self.assertEqual(json.loads(descriptor.read_text())['model'], 'grok-exact')
        self.assertNotIn('长任务', str(popen.call_args.args[0]))
        self.assertEqual(popen.call_args.kwargs['env']['PYINSTALLER_RESET_ENVIRONMENT'], '1')
        self.assertEqual((descriptor.parent / 'prompt.txt').read_text('utf-8'), '长任务' * 8000)

    def test_runner_checks_exit_and_original_session(self):
        directory = self.root / 'run'
        directory.mkdir()
        request = directory / 'request.json'
        request.write_text(json.dumps({'cli': 'fixture-cli', 'home': str(self.home), 'cwd': str(self.root),
            'session_id': SESSION, 'model': 'grok-exact'}))
        (directory / 'prompt.txt').write_text('task')
        def fake_run(command, **kwargs):
            self.assertEqual(command[1:5], ['--resume', SESSION, '--model', 'grok-exact'])
            self.assertIn('--prompt-file', command)
            self.assertNotIn('--always-approve', command)
            kwargs['stdout'].write(json.dumps({'sessionId': SESSION, 'stopReason': 'EndTurn'}))
            return subprocess.CompletedProcess(command, 0)
        with mock.patch('grok_backend.subprocess.run', side_effect=fake_run):
            run_cli(request)
        self.assertTrue(json.loads((directory / 'result.json').read_text('utf-8'))['ok'])
        def wrong_session(command, **kwargs):
            kwargs['stdout'].write(json.dumps({'sessionId': 'another-session', 'stopReason': 'EndTurn'}))
            return subprocess.CompletedProcess(command, 0)
        with mock.patch('grok_backend.subprocess.run', side_effect=wrong_session):
            run_cli(request)
        self.assertFalse(json.loads((directory / 'result.json').read_text('utf-8'))['ok'])

    def test_resumed_event_counters_do_not_collide_or_match_previous_identical_text(self):
        def user(milliseconds):
            entry = update('user_message_chunk', 'repeat task', 'reused-event')
            entry['params']['_meta'] = {'eventId': 'reused-event', 'agentTimestampMs': milliseconds}
            return entry
        chat = self.data.chats()[0]
        self.write_updates(user(1000))
        old = self.data.history(chat)[0]
        receipt = {'text': 'repeat task', 'baseline_ids': [old.id], 'sent_at_ms': 1500}
        self.write_updates(user(1000), user(2000))
        messages = self.data.history(chat)
        self.assertNotEqual(messages[0].id, messages[1].id)
        self.assertEqual(self.data.receipt_message(receipt, messages), messages[1])
        receipt.update(baseline_ids=['reused-event'], message_id='reused-event')
        self.assertEqual(self.data.receipt_message(receipt, messages), messages[1])
        self.assertIsNone(self.data.receipt_message(receipt, messages[:1]))

    def test_completed_output_recovers_interrupted_supervisor_but_partial_or_wrong_session_does_not(self):
        self.assertIsNone(run_status(self.root, SESSION))
        output = self.root / 'output.json'
        output.write_text('{"sessionId":')
        self.assertIsNone(run_status(self.root, SESSION))
        output.write_text(json.dumps({'sessionId': 'other', 'stopReason': 'EndTurn'}))
        self.assertIsNone(run_status(self.root, SESSION))
        output.write_text(json.dumps({'sessionId': SESSION, 'stopReason': 'EndTurn', 'requestId': 'prompt-a'}))
        self.assertTrue(run_status(self.root, SESSION)['ok'])
        self.assertEqual(run_status(self.root, SESSION)['prompt_id'], 'prompt-a')


if __name__ == '__main__':
    unittest.main()
