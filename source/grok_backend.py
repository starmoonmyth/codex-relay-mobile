from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

from backend import Chat, DeliveryError, Message
from claude_backend import ClaudeData, validate_model


def default_grok_home():
    return pathlib.Path(os.environ.get('GROK_HOME', str(pathlib.Path.home() / '.grok')))


def discover_grok_cli():
    names = ['grok.exe'] if os.name == 'nt' else ['grok']
    for name in names:
        for candidate in [default_grok_home() / 'bin' / name, pathlib.Path.home() / '.local/bin' / name,
                          pathlib.Path('/opt/homebrew/bin') / name, pathlib.Path('/usr/local/bin') / name]:
            if candidate.is_file():
                return str(candidate)
        found = shutil.which(name)
        if found:
            return found
    return ''


def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return fallback


class GrokData(ClaudeData):
    def __init__(self, home=None):
        self.home = pathlib.Path(home or default_grok_home())
        self.projects = self.home / 'sessions'

    def has_sessions(self):
        return next(self.projects.glob('*/*/summary.json'), None) is not None

    def model_choices(self):
        cache = read_json(self.home / 'models_cache.json', {})
        models = cache.get('models') or {}
        choices = [name for name, item in models.items() if not item.get('info', {}).get('hidden')]
        try:
            config = tomllib.loads((self.home / 'config.toml').read_text('utf-8'))
            choices.extend(config.get('model', {}).keys())
        except (OSError, ValueError):
            pass
        return list(dict.fromkeys(choices))

    def chats(self, archived=False):
        chats = []
        for path in self.projects.glob('*/*/summary.json'):
            item = read_json(path, {})
            if not item or item.get('session_kind') == 'subagent':
                continue
            info = item.get('info') or {}
            identity, cwd = info.get('id') or path.parent.name, info.get('cwd')
            if not re.fullmatch(r'[a-fA-F0-9-]{36}', identity) or not isinstance(cwd, str) or not cwd:
                continue
            title = item.get('generated_title') or item.get('session_summary') or 'Grok 聊天'
            updated = path.stat().st_mtime
            try:
                updated = datetime.datetime.fromisoformat(item.get('updated_at', '').replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
            chats.append(Chat('grok:' + identity, ' '.join(str(title).split())[:180], cwd,
                              str(path.parent / 'updates.jsonl'), int(updated),
                              model=item.get('current_model_id') or '', provider='grok'))
        return sorted(chats, key=lambda chat: chat.updated_at, reverse=True)

    def history(self, chat, limit=60):
        messages, current, model = [], None, ''
        for index, entry in enumerate(self._read(pathlib.Path(chat.rollout_path), max_bytes=4 * 1024 * 1024)):
            params = entry.get('params') or {}
            update, meta = params.get('update') or {}, params.get('_meta') or {}
            kind = update.get('sessionUpdate')
            if kind not in ('user_message_chunk', 'agent_message_chunk'):
                current = None
                continue
            content = update.get('content') or {}
            if content.get('type') != 'text' or not isinstance(content.get('text'), str):
                continue
            role = 'user' if kind == 'user_message_chunk' else 'assistant'
            if role == 'user':
                model = (update.get('_meta') or {}).get('modelId') or model
            text = content['text']
            event = str(meta.get('eventId') or f"{entry.get('timestamp', 0)}:{index}")
            group = (role, meta.get('promptId'), meta.get('streamStartMs'))
            if role == 'assistant' and current and current[0] == group:
                old = messages[-1]
                messages[-1] = Message(old.id, role, old.text + text, ordinal=old.ordinal, model=old.model)
            else:
                messages.append(Message(event, role, text, ordinal=index, model=model if role == 'assistant' else ''))
            current = (group, event)
        return messages[-limit:]

    def turn(self, thread_id, turn_id=''):
        chat = next((chat for chat in self.chats() if chat.id == thread_id), None)
        if not chat:
            return {}
        status, identity = '', ''
        for entry in self._read(pathlib.Path(chat.rollout_path), max_bytes=2 * 1024 * 1024):
            params = entry.get('params') or {}
            update = params.get('update') or {}
            kind = update.get('sessionUpdate')
            if kind == 'user_message_chunk':
                status = 'inProgress'
                identity = (params.get('_meta') or {}).get('eventId') or str(entry.get('timestamp', ''))
            elif kind == 'turn_completed':
                reason = str(update.get('stop_reason', '')).lower()
                status = 'completed' if reason in ('end_turn', 'endturn') else 'interrupted' if 'cancel' in reason else 'failed'
        waiting = False
        for event in self._read(pathlib.Path(chat.rollout_path).with_name('events.jsonl'), max_bytes=128 * 1024):
            if event.get('type') in ('turn_started', 'permission_resolved', 'turn_ended'):
                waiting = False
            elif event.get('type') == 'permission_requested':
                waiting = True
        if waiting and status == 'inProgress':
            status = 'needsInput'
        return {'status': status, 'turn_id': identity} if status else {}

    def receipt_status(self, receipt, messages, queue_ids):
        if receipt.get('result') == 'failed':
            return receipt.get('status', '执行失败')
        if receipt.get('grok_finished'):
            return '已完成'
        return '处理中' if self.receipt_message(receipt, messages) else receipt.get('status', '等待接收')


def deliver_grok(cli, home, chat, text, request_id, run_dir, model, permission_mode=''):
    validate_model(model)
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
        raise ValueError('消息编号无效。')
    if not cli or not pathlib.Path(cli).is_file():
        raise ValueError('未找到 Grok CLI，请先在电脑安装并登录 Grok。')
    if not pathlib.Path(chat.cwd).is_dir():
        raise ValueError('Grok 聊天的项目目录不存在。')
    directory = pathlib.Path(run_dir) / request_id
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        directory.chmod(0o700)
    prompt = directory / 'prompt.txt'
    prompt.write_text(text, 'utf-8')
    payload = {'cli': cli, 'home': str(home), 'cwd': chat.cwd, 'session_id': chat.id.split(':', 1)[1],
               'model': model, 'permission_mode': permission_mode}
    request = directory / 'request.json'
    request.write_text(json.dumps(payload), 'utf-8')
    command = [sys.executable, '--grok-run'] if getattr(sys, 'frozen', False) else [sys.executable, str(pathlib.Path(__file__).resolve())]
    try:
        process = subprocess.Popen(command + [str(request)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1'),
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
    except OSError as error:
        raise DeliveryError('无法启动 Grok：' + str(error)) from error
    return {'warning': False, 'queue_ids': [], 'delivery_mode': 'grok-cli', 'process_id': process.pid}


def run_cli(request):
    directory = pathlib.Path(request).parent
    data = read_json(pathlib.Path(request), {})
    command = [data['cli'], '--resume', data['session_id'], '--model', data['model'],
               '--prompt-file', str(directory / 'prompt.txt'), '--output-format', 'json']
    if data.get('permission_mode'):
        command += ['--permission-mode', data['permission_mode']]
    try:
        with (directory / 'output.json').open('w', encoding='utf-8') as output, (directory / 'stderr.log').open('w', encoding='utf-8') as error:
            result = subprocess.run(command, cwd=data['cwd'], env=dict(os.environ, GROK_HOME=data['home']),
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        response = read_json(directory / 'output.json', {})
        same_session = response.get('sessionId') == data['session_id']
        done = result.returncode == 0 and same_session and str(response.get('stopReason', '')).lower() in ('endturn', 'end_turn')
        status = {'ok': done, 'error': '' if done else 'Grok 未完成任务，请在电脑检查登录、模型或工具权限。',
                  'exit_code': result.returncode, 'session_confirmed': same_session}
    except Exception:
        status = {'ok': False, 'error': 'Grok 运行失败，请在电脑检查 CLI 和项目目录。'}
    temporary = directory / 'result.tmp'
    temporary.write_text(json.dumps(status, ensure_ascii=False), 'utf-8')
    temporary.replace(directory / 'result.json')


if __name__ == '__main__':
    run_cli(sys.argv[-1])
