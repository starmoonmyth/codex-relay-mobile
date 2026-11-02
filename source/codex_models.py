from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time


def list_models(cli, home):
    process = subprocess.Popen([cli, 'app-server', '--listen', 'stdio://'],
        env=dict(os.environ, CODEX_HOME=str(home), NO_COLOR='1'),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                try:
                    lines.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            lines.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + 25
    def write(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    def request(identity, method, params):
        write({'id': identity, 'method': method, 'params': params})
        while True:
            item = lines.get(timeout=max(.01, deadline - time.monotonic()))
            if item is None or 'error' in item:
                raise ValueError('无法读取 Codex 模型目录，可填写服务商支持的模型 ID。')
            if item.get('id') == identity:
                return item.get('result', {})
    try:
        request(1, 'initialize', {'clientInfo': {'name': 'codex_relay_mobile', 'version': '1.3.7'}})
        write({'method': 'initialized'})
        models, cursor, identity = [], None, 2
        while True:
            page = request(identity, 'model/list', {'limit': 100, 'cursor': cursor})
            models.extend(item['model'] for item in page.get('data', []) if item.get('model') and not item.get('hidden'))
            cursor = page.get('nextCursor')
            if not cursor:
                return list(dict.fromkeys(models))
            identity += 1
    except (queue.Empty, OSError) as error:
        raise ValueError('Codex 模型目录暂不可用，可填写服务商支持的模型 ID。') from error
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=1)
        process.stdout.close()
