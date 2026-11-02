from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

from backend import DeliveryError, build_command


def deliver_images(cli, home, chat, text, paths, request_id):
    build_command(cli, chat, text or "图片")
    environment = dict(os.environ, CODEX_HOME=str(home), NO_COLOR="1")
    process = None
    submitted = False
    lines = queue.Queue()
    try:
        process = subprocess.Popen([cli, "app-server", "--listen", "stdio://"],
            cwd=chat.cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

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

        def write(value):
            process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            process.stdin.flush()

        def request(identity, method, params):
            write({"id": identity, "method": method, "params": params})
            deadline = time.monotonic() + 40
            while True:
                item = lines.get(timeout=max(0.01, deadline - time.monotonic()))
                if item is None:
                    raise DeliveryError("Codex 图片投递进程已退出，请检查原聊天。", submitted)
                if item.get("id") != identity:
                    if time.monotonic() >= deadline:
                        raise queue.Empty
                    continue
                if "error" in item:
                    raise DeliveryError("Codex 图片投递失败：" + str(item["error"].get("message", "未知错误")), False)
                return item.get("result", {})

        request(1, "initialize", {"clientInfo": {"name": "codex_relay_mobile", "version": "1.3.2"},
                                   "capabilities": {"experimentalApi": True}})
        write({"method": "initialized"})
        inputs = ([{"type": "text", "text": text}] if text else [])
        inputs += [{"type": "localImage", "path": str(path)} for path in paths]
        submitted = True
        result = request(2, "thread/queue/add", {"threadId": chat.id,
            "clientUserMessageId": request_id, "input": inputs})
        identity = result.get("queuedSubmission", {}).get("id")
        if not identity:
            raise DeliveryError("图片投递未返回队列回执，请先检查原聊天。", True)
        return {"warning": False, "queue_ids": [identity]}
    except queue.Empty as error:
        raise DeliveryError("图片投递超时，可能已入队。请先检查原聊天，避免重复发送。", submitted) from error
    except OSError as error:
        raise DeliveryError(f"无法启动图片投递：{error}", submitted) from error
    finally:
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                reader.join(timeout=1)
                process.stdout.close()
