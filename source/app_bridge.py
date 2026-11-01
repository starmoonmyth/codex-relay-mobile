from __future__ import annotations

import json
import os
import pathlib
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from backend import DeliveryError, default_home


class DesktopError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def endpoints(home):
    if os.name == 'nt':
        return [r'\\.\pipe\codex-ipc']
    candidates = [pathlib.Path(home) / 'ipc/ipc.sock', pathlib.Path(tempfile.gettempdir()) / 'codex-ipc' /
                  (f'ipc-{os.getuid()}.sock' if os.getuid() else 'ipc.sock')]
    return [str(path) for path in candidates if path.is_socket() and path.stat().st_uid == os.getuid()]


class DesktopClient:
    # Route user input through the desktop thread owner, preserving native turn handling.
    VERSIONS = {'initialize': 0, 'thread-owner-discovery': 1,
                'thread-follower-steer-turn': 1, 'thread-follower-start-turn': 2,
                'thread-follower-remove-queued-message': 1}

    def __init__(self, path):
        if os.name == 'nt':
            self.connection = open(path, 'r+b', buffering=0)
            self.read, self.write = self.connection.read, self.connection.write
        else:
            self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.connection.settimeout(20)
            try:
                self.connection.connect(path)
            except Exception:
                self.connection.close()
                raise
            self.read, self.write = self.connection.recv, self.connection.send
        self.client_id = 'initializing-client'

    def close(self):
        self.connection.close()

    def send(self, value):
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        remaining = memoryview(struct.pack('<I', len(raw)) + raw)
        while remaining:
            count = self.write(remaining)
            if not count:
                raise OSError('桌面连接已关闭。')
            remaining = remaining[count:]

    def exact(self, length):
        output = bytearray()
        while len(output) < length:
            part = self.read(length - len(output))
            if not part:
                raise OSError('桌面连接已关闭。')
            output.extend(part)
        return output

    def receive(self):
        length = struct.unpack('<I', self.exact(4))[0]
        if not 0 < length <= 64 * 1024 * 1024:
            raise ValueError('桌面消息长度无效。')
        return json.loads(self.exact(length))

    def request(self, method, params, owner=None):
        version = self.VERSIONS[method]
        identity = str(uuid.uuid4())
        mutation = method.startswith('thread-follower-')
        value = {'type': 'request', 'requestId': identity, 'sourceClientId': self.client_id,
                 'version': version, 'method': method, 'params': params, 'timeoutMs': 15000}
        if owner:
            value['targetClientId'] = owner
        try:
            self.send(value)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                reply = self.receive()
                if reply.get('type') == 'client-discovery-request':
                    self.send({'type': 'client-discovery-response', 'requestId': reply['requestId'],
                               'response': {'canHandle': False}})
                elif reply.get('type') == 'response' and reply.get('requestId') == identity:
                    if reply.get('resultType') != 'success':
                        error = str(reply.get('error') or '桌面请求失败。')
                        inactive = f"Cannot steer conversation {params.get('conversationId')} because its active turn already ended"
                        rejected = error in {'no-client-found', 'request-version-mismatch',
                                             'no-handler-for-request', 'no active turn to steer', inactive}
                        raise DesktopError(error, mutation and not rejected)
                    if reply.get('method') != method or (owner and reply.get('handledByClientId') != owner):
                        raise DesktopError('桌面回执与目标窗口不一致。', mutation)
                    return reply
            raise TimeoutError('桌面回复超时。')
        except (OSError, ValueError) as error:
            raise DesktopError(str(error), mutation) from error

    def initialize(self):
        reply = self.request('initialize', {'clientType': 'codex-relay-mobile'})
        self.client_id = reply.get('result', {}).get('clientId')
        if not isinstance(self.client_id, str) or not self.client_id:
            raise DesktopError('桌面未返回连接编号。')


def submit_answer(client, request):
    thread_id, text, request_id = request['thread_id'], request['text'], request['request_id']
    try:
        owner = client.request('thread-owner-discovery', {'hostId': 'local', 'conversationId': thread_id})
    except DesktopError as error:
        if str(error) == 'no-client-found':
            raise DesktopError('请在 Codex 桌面中打开这个聊天后，再提交回复。本次未发送。') from error
        raise
    owner_id = owner.get('handledByClientId')
    if not owner_id:
        raise DesktopError('找不到持有聊天的桌面窗口，请在 Codex 中打开这个聊天。')
    for queue_id in request.get('queue_ids', []):
        removed = client.request('thread-follower-remove-queued-message',
                                 {'conversationId': thread_id, 'messageId': queue_id}, owner_id)
        if not removed.get('result', {}).get('removed'):
            raise DesktopError('旧回复可能已被桌面取出，请检查原聊天，勿重复提交。', True)
    inputs = [{'type': 'text', 'text': text, 'text_elements': []}]
    restore = {'id': request_id, 'text': text, 'cwd': request['cwd'], 'createdAt': int(time.time() * 1000),
               'context': {'prompt': text, 'addedFiles': [], 'fileAttachments': [], 'ideContext': None,
                           'imageAttachments': [], 'workspaceRoots': [request['cwd']]}}
    try:
        reply = client.request('thread-follower-steer-turn', {
            'conversationId': thread_id, 'input': inputs, 'restoreMessage': restore,
            'attachments': [], 'clientUserMessageId': request_id}, owner_id)
    except DesktopError as error:
        inactive = f'Cannot steer conversation {thread_id} because its active turn already ended'
        if str(error) not in {'no active turn to steer', inactive} or error.uncertain:
            raise
        reply = client.request('thread-follower-start-turn', {
            'conversationId': thread_id,
            'turnStart': {'request': {'threadId': thread_id, 'input': inputs,
                                      'clientUserMessageId': request_id},
                          'context': {'restoreMessage': restore}}}, owner_id)
    result = reply.get('result', {}).get('result')
    turn_id = (result.get('turnId') or result.get('turn', {}).get('id')) if isinstance(result, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise DesktopError('桌面未返回轮次接收回执，请检查原聊天后再决定是否重发。', True)
    return {'ok': True, 'turn_id': turn_id}


def run_helper(request):
    client = None
    for path in endpoints(request.get('home') or default_home()):
        try:
            candidate = DesktopClient(path)
            try:
                candidate.initialize()
            except Exception:
                candidate.close()
                raise
            client = candidate
            break
        except (OSError, ValueError, DesktopError):
            continue
    if client is None:
        raise DesktopError('无法连接 Codex 桌面，请打开桌面应用和这个聊天后重试。本次未发送。')
    try:
        return submit_answer(client, request)
    finally:
        client.close()


def deliver_answer(chat, text, request_id, home=None, queue_ids=None):
    command = [sys.executable, '--app-bridge'] if getattr(sys, 'frozen', False) else [sys.executable, str(pathlib.Path(__file__).resolve())]
    with tempfile.TemporaryDirectory(prefix='codex-relay-answer-') as directory:
        source, destination = pathlib.Path(directory) / 'request.json', pathlib.Path(directory) / 'response.json'
        source.write_text(json.dumps({'thread_id': chat.id, 'cwd': chat.cwd, 'text': text,
                                      'request_id': request_id, 'home': str(home or default_home()),
                                      'queue_ids': list(queue_ids or [])}, ensure_ascii=False), 'utf-8')
        if os.name != 'nt':
            source.chmod(0o600)
        try:
            subprocess.run(command + [str(source), str(destination)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=50,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired as error:
            raise DeliveryError('桌面回复超时，可能已接收。请检查原聊天，勿重复提交。', True) from error
        if not destination.is_file():
            raise DeliveryError('桌面未返回回执，请检查原聊天，勿重复提交。', True)
        result = json.loads(destination.read_text('utf-8'))
        if not result.get('ok'):
            raise DeliveryError(result.get('error') or '桌面即时回复失败。', bool(result.get('uncertain')))
    return {'warning': False, 'queue_ids': [], 'delivery_mode': 'immediate', 'turn_id': result['turn_id']}


def helper_main():
    source, destination = map(pathlib.Path, sys.argv[-2:])
    try:
        result = run_helper(json.loads(source.read_text('utf-8')))
    except DesktopError as error:
        result = {'ok': False, 'error': str(error), 'uncertain': error.uncertain}
    except Exception:
        result = {'ok': False, 'error': '桌面投递异常，请检查原聊天，勿重复提交。', 'uncertain': True}
    destination.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')


if __name__ == '__main__':
    helper_main()
