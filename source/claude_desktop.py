from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

from backend import DeliveryError, MAX_MESSAGE
from desktop_records import ClaudeDesktopRecords, claude_desktop_roots


_send_lock = threading.Lock()
_compile_lock = threading.Lock()
ASSETS = pathlib.Path(__file__).resolve().parent / 'native'


def desktop_target(chat, roots=None):
    sessions = ClaudeDesktopRecords(claude_desktop_roots() if roots is None else roots).sessions()
    matches = []
    for cli_id, views in sessions.items():
        for view in views:
            if (chat.id == 'claude:' + cli_id or chat.id == 'claude-desktop:' + str(view.get('sessionId'))):
                matches.append((cli_id, view))
    if len(matches) != 1:
        raise ValueError('无法唯一确认 Claude 桌面聊天，请刷新列表后重新选择。本次未发送。')
    cli_id, view = matches[0]
    native_id = view.get('sessionId', '')
    if not re.fullmatch(r'local_[a-fA-F0-9-]{36}', native_id):
        raise ValueError('Claude 桌面会话标识无效，本次未发送。')
    if chat.source != 'claude-desktop' or pathlib.Path(chat.rollout_path).stem != cli_id:
        raise ValueError('Claude 桌面记录身份已变化，请重新选择。本次未发送。')
    if view.get('isArchived'):
        raise ValueError('Claude 桌面聊天已归档，请先在桌面恢复。')
    return {'native_id': native_id, 'view_path': view['_record_path'],
            'expected_head': view.get('lastAssistantUuid') or '', 'expected_cli': cli_id}


def _windows_helper(directory):
    source = ASSETS / 'ClaudeDesktop.cs'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    helper = pathlib.Path(directory) / ('claude-desktop-' + digest + '.exe')
    with _compile_lock:
        if helper.is_file():
            return helper
        framework = pathlib.Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Microsoft.NET/Framework64/v4.0.30319'
        compiler = framework / 'csc.exe'
        if not compiler.is_file():
            raise FileNotFoundError('桌面代发需要 Windows 自带的 .NET Framework 4.x。')
        helper.parent.mkdir(parents=True, exist_ok=True)
        command = [str(compiler), '/nologo', '/target:exe', '/out:' + str(helper),
                   '/reference:' + str(framework / 'WPF/UIAutomationClient.dll'),
                   '/reference:' + str(framework / 'WPF/UIAutomationTypes.dll'),
                   '/reference:' + str(framework / 'WPF/WindowsBase.dll'),
                   '/reference:System.Web.Extensions.dll', '/reference:System.Windows.Forms.dll', str(source)]
        result = subprocess.run(command, capture_output=True, timeout=45,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise DeliveryError('Claude 桌面代发组件未能准备好，本次未发送。')
    return helper


def run_desktop(payload, directory):
    if os.name == 'nt':
        command = [str(_windows_helper(directory))]
    elif sys.platform == 'darwin':
        subprocess.run(['/usr/bin/open', 'claude://claude.ai/epitaxy/' + payload['native_id']], check=True,
                       capture_output=True, timeout=10)
        command = ['/usr/bin/osascript', '-l', 'JavaScript', str(ASSETS / 'claude_desktop.js')]
    else:
        raise ValueError('Claude 桌面代发目前支持 Windows 与 macOS。')
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                                capture_output=True, timeout=50,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as error:
        raise DeliveryError('Claude 桌面响应超时，请先核对原聊天再重试。', uncertain=True) from error
    try:
        reply = json.loads(result.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as error:
        raise DeliveryError('Claude 桌面代发未返回确认，请先核对原聊天。', uncertain=True) from error
    if not reply.get('ok'):
        raise DeliveryError(str(reply.get('error') or 'Claude 桌面未接收消息。'), uncertain=reply.get('uncertain', False))
    return reply


def deliver_desktop(chat, text, image_paths, request_id, run_dir, roots=None, runner=run_desktop):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
        raise ValueError('请输入要发送的文字。')
    if image_paths:
        raise ValueError('Claude 桌面代发当前支持文字，图片请在原生桌面添加。')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
        raise ValueError('消息编号无效。')
    with _send_lock:
        target = desktop_target(chat, roots)
        native_id = target['native_id']
        # The helper never presses Send twice. A durable claim also covers a
        # service crash after the native app accepted the message.
        directory = pathlib.Path(run_dir)
        directory.mkdir(parents=True, exist_ok=True)
        claim = directory / (request_id + '.json')
        if claim.exists():
            previous = json.loads(claim.read_text('utf-8'))
            if previous.get('native_id') != native_id or previous.get('text_hash') != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError('此消息编号已用于其他内容。')
            if previous.get('result'):
                return previous['result']
            raise DeliveryError('这条消息已经尝试投递，请先核对 Claude 原聊天，避免重复发送。', uncertain=True)
        payload = dict(target, text=text)
        record = {'native_id': native_id, 'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'created': time.time()}
        with claim.open('x', encoding='utf-8') as handle:
            json.dump(record, handle)
        reply = runner(payload, directory)
        result = {'warning': False, 'queue_ids': [], 'delivery_mode': 'claude-desktop',
                  'requested_model': reply.get('model') or ''}
        record['result'] = result
        claim.write_text(json.dumps(record), 'utf-8')
        return result
