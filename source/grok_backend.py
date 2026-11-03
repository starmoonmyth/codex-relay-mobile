from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
import time

from backend import Chat, DeliveryError, Message
from claude_backend import ClaudeData, validate_model
from desktop_records import GrokBotRecords


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


def event_time(entry):
    meta = (entry.get('params') or {}).get('_meta') or {}
    return int(meta.get('agentTimestampMs') or (entry.get('timestamp') or 0) * 1000)


def event_id(entry, index=0):
    meta = (entry.get('params') or {}).get('_meta') or {}
    # Grok restarts event counters when a CLI resumes the same session.
    identity = [meta.get('eventId'), meta.get('promptId'), event_time(entry)]
    if not any(identity):
        identity.append(index)
    return 'grok-message:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def native_turn_state(chat, status, identity=''):
    """Reconcile update streams with native terminal events and live sessions."""
    path = pathlib.Path(chat.rollout_path)
    events = ClaudeData._read(path.with_name('events.jsonl'), max_bytes=256 * 1024)
    newest_user = 0
    for entry in ClaudeData._read(path, max_bytes=2 * 1024 * 1024):
        if ((entry.get('params') or {}).get('update') or {}).get('sessionUpdate') == 'user_message_chunk':
            newest_user = event_time(entry)
    waiting = False
    for event in events:
        try:
            stamp = datetime.datetime.fromisoformat(event.get('ts', '').replace('Z', '+00:00')).timestamp() * 1000
        except ValueError:
            continue
        if stamp < newest_user:
            continue
        kind = event.get('type')
        if kind in ('turn_started', 'permission_resolved', 'turn_ended'):
            waiting = False
        if kind == 'permission_requested':
            waiting = True
        elif kind == 'turn_ended':
            outcome = str(event.get('outcome') or '').lower()
            if 'cancel' in outcome or 'interrupt' in outcome or 'abort' in outcome:
                status = 'interrupted'
            elif outcome in ('completed', 'success', 'succeeded', 'end_turn'):
                status = 'completed'
            elif outcome in ('failed', 'error'):
                status = 'failed'
    if status == 'inProgress':
        registry = path.parents[3] / 'active_sessions.json'
        active = read_json(registry)
        if isinstance(active, list):
            session = chat.id.split(':', 1)[1]
            alive = any(row.get('session_id') == session and process_alive(row.get('pid'))
                        for row in active if isinstance(row, dict))
            recent = max(path.stat().st_mtime, path.with_name('events.jsonl').stat().st_mtime if path.with_name('events.jsonl').exists() else 0)
            if not alive and time.time() - recent > 10:
                status = 'interrupted'
                waiting = False
    return {'status': 'needsInput' if waiting and status == 'inProgress' else status, 'turn_id': identity}


def run_status(directory, session_id):
    result = read_json(directory / 'result.json')
    if isinstance(result, dict) and isinstance(result.get('ok'), bool):
        return result
    output = read_json(directory / 'output.json', {})
    if (isinstance(output, dict) and output.get('sessionId') == session_id and
            str(output.get('stopReason', '')).lower() in ('endturn', 'end_turn')):
        return {'ok': True, 'session_confirmed': True, 'prompt_id': output.get('requestId')}
    return None


class GrokData(ClaudeData):
    def __init__(self, home=None, bot_root=None):
        self.home = pathlib.Path(home or default_grok_home())
        self.projects = self.home / 'sessions'
        self.bot = GrokBotRecords(bot_root) if bot_root is not None or self.home == default_grok_home() else None

    def has_sessions(self):
        return next(self.projects.glob('*/*/summary.json'), None) is not None or bool(self.bot and self.bot.chats())

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
            if identity != path.parent.name:
                continue
            title = item.get('generated_title') or item.get('session_summary') or 'Grok 聊天'
            updated = path.stat().st_mtime
            try:
                updated = datetime.datetime.fromisoformat(item.get('updated_at', '').replace('Z', '+00:00')).timestamp()
            except ValueError:
                pass
            chats.append(Chat('grok:' + identity, ' '.join(str(title).split())[:180], cwd,
                              str(path.parent / 'updates.jsonl'), int(updated),
                              model=item.get('current_model_id') or '', provider='grok', source='grok-cli'))
        counts = {}
        for chat in chats:
            counts[chat.id] = counts.get(chat.id, 0) + 1
        chats = [chat for chat in chats if counts[chat.id] == 1]
        if self.bot:
            chats.extend(self.bot.chats())
        return sorted(chats, key=lambda chat: chat.updated_at, reverse=True)

    def history(self, chat, limit=60):
        if chat.source == 'grok-bot':
            return self.bot.history(chat, limit) if self.bot else []
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
            event = event_id(entry, index)
            group = (role, meta.get('promptId'), meta.get('streamStartMs'))
            if role == 'assistant' and current and current[0] == group:
                old = messages[-1]
                messages[-1] = Message(old.id, role, old.text + text, ordinal=old.ordinal, model=old.model, timestamp_ms=old.timestamp_ms)
            else:
                messages.append(Message(event, role, text, ordinal=index, model=model if role == 'assistant' else '', timestamp_ms=event_time(entry)))
            current = (group, event)
        return messages[-limit:] if limit else messages

    @staticmethod
    def receipt_message(receipt, messages):
        baseline = set(receipt.get('baseline_ids', []))
        sent = receipt.get('sent_at_ms') or 0
        for message in messages:
            if message.role != 'user' or message.id in baseline:
                continue
            if sent and message.timestamp_ms and message.timestamp_ms < sent:
                continue
            if message.text.rstrip('\r\n') == str(receipt.get('text', '')).rstrip('\r\n'):
                if not receipt.get('message_id') or not receipt['message_id'].startswith('grok-message:') or message.id == receipt['message_id']:
                    return message
        return None

    def historical_receipt_message(self, receipt):
        chat = next((chat for chat in self.chats() if chat.id == receipt['thread_id']), None)
        return self.receipt_message(receipt, self.history(chat, limit=0)) if chat else None

    def turn(self, thread_id, turn_id=''):
        chat = next((chat for chat in self.chats() if chat.id == thread_id), None)
        if not chat:
            return {}
        if chat.source == 'grok-bot':
            return {}
        status, identity = '', ''
        for index, entry in enumerate(self._read(pathlib.Path(chat.rollout_path), max_bytes=2 * 1024 * 1024)):
            params = entry.get('params') or {}
            update = params.get('update') or {}
            kind = update.get('sessionUpdate')
            if kind == 'user_message_chunk':
                status = 'inProgress'
                identity = event_id(entry, index)
            elif kind == 'turn_completed':
                reason = str(update.get('stop_reason', '')).lower()
                status = 'completed' if reason in ('end_turn', 'endturn') else 'interrupted' if 'cancel' in reason else 'failed'
        return native_turn_state(chat, status, identity) if status else {}

    def receipt_status(self, receipt, messages, queue_ids):
        if receipt.get('result') == 'failed':
            return receipt.get('status', '执行失败')
        if receipt.get('grok_finished'):
            return '已完成'
        if receipt.get('native_status') in ('interrupted', 'failed', 'needs_input'):
            return {'interrupted': '已中断', 'failed': '执行失败', 'needs_input': '待你回复'}[receipt['native_status']]
        return '处理中' if self.receipt_message(receipt, messages) else receipt.get('status', '等待接收')


def validate_target(home, chat):
    path = pathlib.Path(chat.rollout_path).resolve()
    projects = pathlib.Path(home).resolve() / 'sessions'
    session = chat.id.split(':', 1)[1]
    summary = read_json(path.with_name('summary.json'), {})
    info = summary.get('info') or {}
    if (not path.is_relative_to(projects) or path.parent.name != session or info.get('id') != session or
            pathlib.Path(info.get('cwd') or '').resolve() != pathlib.Path(chat.cwd).resolve()):
        raise ValueError('Grok 会话的来源、编号或项目已变化，请刷新聊天。本次未发送。')


def deliver_grok(cli, home, chat, text, request_id, run_dir, model, permission_mode=''):
    validate_model(model)
    if not chat.writable or chat.source == 'grok-bot' or not chat.id.startswith('grok:'):
        raise ValueError('这是 Grok Bot 桌面聊天，请在 Grok Bot 中发送。不会转交给另一个 CLI 会话。')
    validate_target(home, chat)
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
               'model': model, 'permission_mode': permission_mode, 'transcript': chat.rollout_path}
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
        if data.get('transcript'):
            chat = Chat('grok:' + data['session_id'], '', data['cwd'], data['transcript'], 0, provider='grok')
            validate_target(data['home'], chat)
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
