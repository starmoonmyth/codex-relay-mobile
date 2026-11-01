from __future__ import annotations

import contextlib
import base64
import dataclasses
import hashlib
import http.cookies
import io
import json
import os
import pathlib
import re
import secrets
import socket
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend import (CodexData, DeliveryError, MAX_MESSAGE, default_home, deliver,
                     discover_cli, display_user_text, new_receipt, state_dir)
from image_delivery import deliver_images
from notifications import Notifications, question_answers
from app_bridge import deliver_answer

STATIC = pathlib.Path(__file__).resolve().parent / "web"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
IMAGE_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def mobile_state():
    return state_dir() / "Mobile"


class Relay:
    def __init__(self, root=None, config=None, sender=deliver, image_sender=deliver_images, answer_sender=deliver_answer):
        self.root = pathlib.Path(root or mobile_state())
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        config_path = self.root / "config.json"
        self.config = config if config is not None else json.loads(config_path.read_text("utf-8"))
        self.port = int(self.config.get("port", 8879))
        self.origin = self.config["origin"].rstrip("/")
        self.local_origin = f"http://127.0.0.1:{self.port}"
        self.origins = {self.origin, self.local_origin}
        self.hosts = {urllib.parse.urlsplit(origin).netloc for origin in self.origins}
        self.data = CodexData(pathlib.Path(self.config.get("codex_home") or default_home()))
        self.cli = self.config.get("cli") or discover_cli()
        self.sender = sender
        self.image_sender = image_sender
        self.answer_sender = answer_sender
        self.lock = threading.Lock()
        self.answer_lock = threading.Lock()
        self.attempts = {}
        self.db = self.root / "mobile.sqlite"
        token_file = self.root / "pairing-token"
        if not token_file.exists():
            with token_file.open("x", encoding="ascii") as handle:
                handle.write(secrets.token_urlsafe(32))
            if os.name != "nt":
                token_file.chmod(0o600)
        self.token = token_file.read_text("ascii").strip()
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY, thread_id TEXT, text TEXT, receipt TEXT, created INTEGER
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER,
                    width INTEGER, height INTEGER, sha256 TEXT, created INTEGER
                );
            """)
        if os.name != "nt":
            self.db.chmod(0o600)
        # An interrupted CLI call may already have queued its message.
        with self.connect() as connection:
            for row in connection.execute("SELECT id,receipt FROM deliveries"):
                receipt = json.loads(row[1])
                if receipt.get("status") == "正在投递":
                    receipt.update(status="投递结果待确认", result="uncertain")
                    self.save_receipt(connection, receipt)
        self.notifications = Notifications(self)

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def session(self, value):
        with self.connect() as connection:
            return bool(connection.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?",
                        (hashlib.sha256(value.encode()).hexdigest(), time.time())).fetchone())

    def pair(self, token, client):
        now = time.time()
        with self.lock:
            times = [stamp for stamp in self.attempts.get(client, []) if stamp > now - 60]
            if len(times) >= 10:
                raise ValueError("尝试次数过多，请一分钟后重试。")
            times.append(now)
            self.attempts[client] = times
            if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                raise ValueError("配对码不正确。请在电脑上重新打开手机连接页。")
            self.attempts.pop(client, None)
        value = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            connection.execute("INSERT INTO sessions VALUES (?,?)",
                               (hashlib.sha256(value.encode()).hexdigest(), now + 30 * 86400))
        return value

    def chat(self, identity):
        return next((chat for chat in self.data.chats() if chat.id == identity), None)

    @staticmethod
    def save_receipt(connection, receipt):
        connection.execute("UPDATE deliveries SET receipt=? WHERE id=?",
                           (json.dumps(receipt, ensure_ascii=False), receipt["id"]))

    def upload(self, name, data):
        from PIL import Image, UnidentifiedImageError
        if not isinstance(data, str) or len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("单张图片最大 8 MB，请缩小后重试。")
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as error:
            raise ValueError("图片内容无效，请重新选择。") from error
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片最大 8 MB。")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in IMAGE_MIMES:
                    raise ValueError("请上传 JPEG、PNG 或 WebP 图片。")
                width, height = image.size
                if width * height > 20000000:
                    raise ValueError("图片尺寸过大，请缩小后重试。")
                mime = IMAGE_MIMES[image.format]
                image.load()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
            raise ValueError("图片损坏或无法读取，请重新选择。") from error
        identity = secrets.token_hex(16)
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(name or "图片").replace("\\", "/").rsplit("/", 1)[-1])[:100] or "图片"
        directory = self.root / "uploads"
        directory.mkdir(exist_ok=True)
        path = directory / (identity + IMAGE_SUFFIX[mime])
        with path.open("xb") as handle:
            handle.write(raw)
        if os.name != "nt":
            path.chmod(0o600)
        try:
            with self.connect() as connection:
                connection.execute("INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?)",
                    (identity, name, mime, len(raw), width, height, hashlib.sha256(raw).hexdigest(), int(time.time())))
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.image(identity)

    def image(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("图片编号无效。")
        with self.connect() as connection:
            row = connection.execute("SELECT id,name,mime,size,width,height,sha256 FROM uploads WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("图片已不存在，请重新选择。")
        result = dict(row)
        result["url"] = "/api/image/" + identity
        if not self.image_path(result).is_file():
            raise ValueError("图片文件已不存在，请重新选择。")
        return result

    def image_path(self, image):
        return self.root / "uploads" / (image["id"] + IMAGE_SUFFIX[image["mime"]])

    def send(self, identity, text, request_id, image_ids=None, immediate=False):
        image_ids = [] if image_ids is None else image_ids
        if not isinstance(image_ids, list) or any(not isinstance(item, str) for item in image_ids):
            raise ValueError("图片列表无效。")
        if not isinstance(text, str) or (not text.strip() and not image_ids) or len(text) > MAX_MESSAGE:
            raise ValueError(f"请输入消息或选择图片；文字最多 {MAX_MESSAGE:,} 字。")
        if not isinstance(request_id, str) or not 16 <= len(request_id) <= 100:
            raise ValueError("消息编号无效，请刷新后重试。")
        with self.lock:
            retry_queue_ids = []
            with self.connect() as connection:
                existing = connection.execute("SELECT * FROM deliveries WHERE id=?", (request_id,)).fetchone()
            if existing:
                old = json.loads(existing["receipt"])
                if existing["thread_id"] != identity or existing["text"] != text or [image["id"] for image in old.get("images", [])] != image_ids:
                    raise ValueError("此消息编号已使用，请检查已发送的消息。")
                if not immediate or old.get('delivery_mode') == 'immediate' or old.get('result') != 'sent':
                    return old
                queued = set(old.get('queue_ids', []))
                chat = self.chat(identity)
                if not queued or not queued <= self.data.queue_ids(identity, text) or not chat or self.data.receipt_message(old, self.data.history(chat)):
                    return old
                retry_queue_ids = sorted(queued)
            chat = self.chat(identity)
            if chat is None:
                raise ValueError("聊天不存在或已归档，请重新选择。")
            images = [self.image(item) for item in image_ids]
            if sum(image['size'] for image in images) > MAX_ATTACHMENT_BYTES:
                raise ValueError('每条消息的图片总大小最多 32 MB，请移除部分图片后发送。')
            messages = self.data.history(chat)
            before = self.data.queue_ids(chat.id, text)
            receipt = new_receipt(chat, text, messages)
            receipt["id"] = request_id
            receipt["images"] = images
            with self.connect() as connection:
                if existing:
                    self.save_receipt(connection, receipt)
                else:
                    connection.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)",
                        (request_id, identity, text, json.dumps(receipt, ensure_ascii=False), int(time.time())))
            try:
                result = (self.answer_sender(chat,text,request_id,self.data.home,retry_queue_ids) if immediate else self.image_sender(self.cli, self.data.home, chat, text,
                          [self.image_path(image) for image in images], request_id) if images
                          else self.sender(self.cli, self.data.home, chat, text))
                receipt.update(result="sent", status="已入队 · 等待回执", warning=result.get("warning", False))
                receipt["queue_ids"] = result.get("queue_ids", [])
                receipt['delivery_mode'] = result.get('delivery_mode','queue')
                if immediate:
                    receipt['turn_id'] = result.get('turn_id')
                    receipt['status'] = '桌面已接收 · 已立即提交'
            except DeliveryError as error:
                receipt.update(result="uncertain" if error.uncertain else "failed",
                               status="投递结果待确认" if error.uncertain else "投递失败", error=str(error))
            except (ValueError, OSError) as error:
                receipt.update(result="failed", status="投递失败", error=str(error))
            except Exception:
                receipt.update(result="uncertain", status="投递结果待确认",
                               error="投递状态读取失败，请先检查原聊天，避免重复发送。")
            if retry_queue_ids and receipt['result'] != 'sent':
                remaining = set(retry_queue_ids) & self.data.queue_ids(chat.id, text)
                if remaining == set(retry_queue_ids) and receipt['result'] == 'failed':
                    receipt.update(result='sent', delivery_mode='queue', queue_ids=retry_queue_ids,
                                   status='旧回复仍在队列，请打开桌面聊天后点击立即提交')
                elif remaining:
                    receipt.update(result='uncertain', queue_ids=sorted(remaining), status='投递结果待确认')
            try:
                receipt["queue_ids"] = list(set(receipt["queue_ids"]) | (self.data.queue_ids(chat.id, text) - before))
            except (sqlite3.Error, OSError):
                pass
            with self.connect() as connection:
                self.save_receipt(connection, receipt)
            return receipt

    def answer(self, identity, message_id, answers, request_id):
        with self.answer_lock:
            snapshot = self.snapshot(identity)
            message = next((item for item in snapshot['messages'] if item['id'] == message_id and item['questions']), None)
            if message is None:
                raise ValueError('找不到这个提问，请刷新聊天。')
            if not isinstance(answers, list) or len(answers) != len(message['questions']) or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in answers):
                raise ValueError('请回答每个问题；每项最多 4000 字。')
            values = [{'questionItemId': json.dumps(['request_user_input_async', message_id, index], separators=(',', ':')),
                       'question': question['title'], 'answer': value.strip()}
                      for index, (question, value) in enumerate(zip(message['questions'], answers))]
            text = '<send_user_message_question_reply>\n' + json.dumps(values, ensure_ascii=False) + '\n</send_user_message_question_reply>'
            # A lost HTTP response can be retried with the same delivery identifier.
            with self.connect() as c:
                existing = c.execute('SELECT text FROM deliveries WHERE id=?', (request_id,)).fetchone()
            if not existing and any(question.get('answer') is not None for question in message['questions']):
                raise ValueError('这个提问已有回复或正在投递，请刷新聊天查看。')
            return self.send(identity, text, request_id, immediate=True)

    def snapshot(self, identity):
        chat = self.chat(identity)
        if chat is None:
            raise ValueError("聊天不存在或已归档。")
        messages = self.data.history(chat)
        queue_ids = self.data.queue_ids(identity)
        with self.connect() as connection:
            receipts = [json.loads(row[0]) for row in connection.execute(
                "SELECT receipt FROM deliveries WHERE thread_id=? ORDER BY created DESC, rowid DESC LIMIT 30", (identity,))]
            for receipt in receipts:
                match = self.data.receipt_message(receipt, messages)
                if not match and receipt.get('result') == 'sent' and not set(receipt.get('queue_ids', [])) & queue_ids:
                    match = self.data.historical_receipt_message(receipt)
                if match:
                    receipt["seen_in_history"] = True
                    receipt['message_id'] = match.id
                    receipt['turn_id'] = match.turn_id
                if receipt["status"] != "正在投递":
                    receipt["status"] = self.data.receipt_status(receipt, messages, queue_ids)
                self.save_receipt(connection, receipt)
        output = []
        answers = {}
        answer_status = {}
        answer_requests = {}
        for message in messages:
            if message.role == 'user':
                actual = question_answers(message.text)
                answers.update(actual)
                answer_status.update({key: 'received' for key in actual})
        for receipt in receipts:
            receipt['display_text'] = display_user_text(receipt['text'])
            if receipt.get('result') != 'failed':
                for key, answer in question_answers(receipt['text']).items():
                    answers.setdefault(key, answer)
                    if receipt.get('delivery_mode') == 'immediate' and receipt.get('result') == 'sent':
                        status = 'submitted'
                    elif receipt.get('result') == 'sent' and set(receipt.get('queue_ids', [])) & queue_ids:
                        status = 'queued'
                    else:
                        status = receipt.get('result', 'uncertain')
                    answer_status.setdefault(key, status)
                    answer_requests.setdefault(key, receipt['id'])
        uploaded = {}
        for receipt in receipts:
            match = self.data.receipt_message(receipt, messages)
            if match and receipt.get("images"):
                uploaded[match.id] = receipt["images"]
        for message in messages:
            item = dataclasses.asdict(message)
            item["images"] = uploaded.get(message.id, [])
            item["image_count"] = len(message.images)
            item["text"] = display_user_text(message.text) if message.role == "user" else message.text
            item['questions'] = [dict(question, index=index, answer=answers.get((message.id, index)),
                                      reply_status=answer_status.get((message.id, index), ''),
                                      reply_request_id=answer_requests.get((message.id, index)))
                                 for index, question in enumerate(message.questions)]
            if item["text"].strip() or message.images or message.questions:
                item["text"] = item["text"][:48000]
                output.append(item)
        return {"chat": dataclasses.asdict(chat), "messages": output,
                "receipts": list(reversed(receipts)), "turn": self.data.turn(identity)}


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, relay):
        self.relay = relay
        super().__init__(("127.0.0.1", relay.port), Handler)
        relay.notifications.start()

    def server_close(self):
        self.relay.notifications.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexRelayMobile/1.2"

    @property
    def relay(self):
        return self.server.relay

    def log_message(self, *_):
        pass

    def local(self):
        forwarded = any(self.headers.get(name) for name in (
            "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "Tailscale-User-Login"))
        return not forwarded and self.headers.get("Host") == urllib.parse.urlsplit(self.relay.local_origin).netloc

    def cookie(self):
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["relay_session"].value if "relay_session" in cookie else ""
        except http.cookies.CookieError:
            return ""

    def response(self, value, status=200, mime="application/json; charset=utf-8", headers=None):
        raw = json.dumps(value, ensure_ascii=False).encode() if mime.startswith("application/json") else value
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.route(False)

    def do_POST(self):
        self.route(True)

    def route(self, post):
        self.connection.settimeout(55)
        if self.headers.get("Host") not in self.relay.hosts:
            self.response({"error": "Host 不受信任。"}, 403)
            return
        try:
            path = urllib.parse.urlsplit(self.path).path
            if post:
                if self.headers.get("Origin") != (self.relay.local_origin if self.local() else self.relay.origin):
                    self.response({"error": "来源校验失败，请从手机连接地址重新打开。"}, 403)
                    return
                if path == "/api/upload" and not self.relay.session(self.cookie()):
                    self.response({"error": "请先配对这台电脑。"}, 401)
                    return
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求需要 JSON 格式。")
                length = int(self.headers.get("Content-Length", "0"))
                maximum = (MAX_IMAGE_BYTES + 2) // 3 * 4 + 1000 if path == "/api/upload" else 100000
                if not 0 < length <= maximum:
                    raise ValueError("请求大小无效。")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效。")
                if path == "/api/pair":
                    value = self.relay.pair(body.get("token"), self.headers.get("Tailscale-User-Login", "local"))
                    secure = "; Secure" if not self.local() else ""
                    self.response({"ok": True}, headers={"Set-Cookie": f"relay_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"})
                    return
                if path == "/api/local/stop" and self.local():
                    self.response({"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if path == '/api/notifications/ack':
                    self.response(self.relay.notifications.acknowledge(body.get('token'),body.get('stage'),body.get('error','')))
                    return
            if path == "/api/health" and not post:
                self.response({"app": "CodexRelayMobile", "version": "1.2.3", "label": self.relay.config.get("label", socket.gethostname())})
                return
            if path.startswith("/api/local/"):
                if not self.local() or post:
                    self.response({"error": "请在电脑本机打开连接页。"}, 403)
                    return
                link = self.relay.origin + "/#pair=" + self.relay.token
                if path == "/api/local/setup":
                    self.response({"link": link, "origin": self.relay.origin, "label": self.relay.config.get("label"), "token": self.relay.token})
                elif path == "/api/local/qr.svg":
                    import qrcode
                    import qrcode.image.svg
                    output = io.BytesIO()
                    qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4).save(output)
                    self.response(output.getvalue(), mime="image/svg+xml")
                else:
                    self.response({"error": "找不到此操作。"}, 404)
                return
            if path.startswith("/api/"):
                if not self.relay.session(self.cookie()):
                    self.response({"error": "请先配对这台电脑。"}, 401)
                    return
                if path == "/api/logout" and post:
                    self.relay.notifications.revoke(self.cookie())
                    with self.relay.connect() as connection:
                        connection.execute("DELETE FROM sessions WHERE hash=?", (hashlib.sha256(self.cookie().encode()).hexdigest(),))
                    self.response({"ok": True}, headers={"Set-Cookie": "relay_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                elif path == "/api/meta" and not post:
                    self.response({"label": self.relay.config.get("label"), "origin": self.relay.origin,
                                   "peers": self.relay.config.get("peers", []), "max_message": MAX_MESSAGE,
                                   "max_attachment_bytes": MAX_ATTACHMENT_BYTES})
                elif path == '/api/notifications' and not post:
                    self.response(self.relay.notifications.settings(self.cookie()))
                elif path == '/api/notifications' and post:
                    self.response(self.relay.notifications.configure(self.cookie(), body.get('enabled'), body.get('subscription')))
                elif path == '/api/notifications/events' and not post:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    self.response(self.relay.notifications.poll(self.cookie(), query.get('after', [0])[0]))
                elif path == '/api/notifications/test' and post:
                    self.response(self.relay.notifications.test(self.cookie()))
                elif path == '/api/notifications/client' and post:
                    self.response(self.relay.notifications.client_info(self.cookie(),body))
                elif path == '/api/notifications/sync' and post:
                    self.response(self.relay.notifications.sync(self.cookie(),body.get('subscription')))
                elif path.startswith('/api/notifications/delivery/') and not post:
                    self.response(self.relay.notifications.delivery(self.cookie(),path.rsplit('/',1)[-1]))
                elif path == '/api/answer' and post:
                    self.response(self.relay.answer(body.get('thread_id'), body.get('message_id'), body.get('answers'), body.get('request_id')))
                elif path == "/api/upload" and post:
                    self.response(self.relay.upload(body.get("name"), body.get("data")))
                elif path.startswith("/api/image/") and not post:
                    picture = self.relay.image(path[len("/api/image/"):])
                    self.response(self.relay.image_path(picture).read_bytes(), mime=picture["mime"])
                elif path == "/api/chats" and not post:
                    self.response({"chats": [dataclasses.asdict(chat) for chat in self.relay.data.chats()]})
                elif path.startswith("/api/chat/") and not post:
                    self.response(self.relay.snapshot(urllib.parse.unquote(path[len("/api/chat/"):])))
                elif path == "/api/send" and post:
                    self.response(self.relay.send(body.get("thread_id"), body.get("text"), body.get("request_id"), body.get("image_ids")))
                else:
                    self.response({"error": "找不到此操作。"}, 404)
                return
            if post:
                self.response({"error": "找不到此操作。"}, 404)
                return
            name = {"/": "index.html", "/connect": "connect.html", "/app.js": "app.js", "/style.css": "style.css",
                    "/connect.js": "connect.js", "/manifest.webmanifest": "manifest.webmanifest", "/sw.js": "sw.js",
                    "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png",
                    "/marked.js": "marked.js", "/purify.js": "purify.js"}.get(path)
            if name is None:
                self.response({"error": "找不到页面。"}, 404)
                return
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json", ".png": "image/png"}[pathlib.Path(name).suffix]
            self.response((STATIC / name).read_bytes(), mime=mime)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self.response({"error": str(error)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, sqlite3.Error) as error:
            self.response({"error": f"读取或投递失败：{error}"}, 503)


def serve(root=None):
    server = HTTPServer(Relay(root))
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
