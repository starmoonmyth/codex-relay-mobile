import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from grok_backend import GrokData, deliver_grok, run_cli, run_status

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
