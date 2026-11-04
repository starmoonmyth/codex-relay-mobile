from __future__ import annotations

import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from server import HTTPServer, Relay, mobile_state
from startup import installed_root, install


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=35, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-2500:])
    return result.stdout


def tailscale():
    candidates = [shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  str(pathlib.Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe")]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    raise RuntimeError("请先安装 Tailscale，并在电脑和手机上登录同一个网络。")


def configure(root):
    program = tailscale()
    status = json.loads(run([program, "status", "--json"]))
    if status.get("BackendState") != "Running":
        raise RuntimeError("请先打开 Tailscale 并连接，再启动手机消息助手。")
    dns = status["Self"]["DNSName"].rstrip(".")
    port = 8879
    https_port = 8444
    config_path = root / "config.json"
    config = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {}
    port = int(config.get("port", port))
    https_port = int(config.get("https_port", https_port))
    rules = json.loads(run([program, "serve", "status", "--json"]))
    existing = rules.get("Web", {}).get(f"{dns}:{https_port}", {}).get("Handlers", {}).get("/", {})
    target = f"http://127.0.0.1:{port}"
    if existing and existing.get("Proxy") != target:
        raise RuntimeError(f"Tailscale 的 {https_port} 端口已用于其他服务。请在 config.json 里选择其他 https_port。")
    if not existing:
        run([program, "serve", "--bg", f"--https={https_port}", target])
    config.update(origin=f"https://{dns}:{https_port}", port=port, https_port=https_port)
    config.setdefault("label", "Mac" if sys.platform == "darwin" else "Windows")
    peers_path = pathlib.Path(__file__).resolve().parent / "peers.json"
    if peers_path.exists():
        config.setdefault("peers", json.loads(peers_path.read_text("utf-8")))
    root.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")
    return config


def error_page(root, error):
    import html
    root.mkdir(parents=True, exist_ok=True)
    path = root / "启动说明.html"
    path.write_text("<!doctype html><meta charset='utf-8'><title>手机消息助手</title>"
                    "<body style='font:18px system-ui;max-width:680px;margin:12vh auto;padding:24px'>"
                    "<h1>手机消息助手暂未启动</h1><p>" + html.escape(str(error)) +
                    "</p><p>处理后重新双击手机消息助手。</p></body>", "utf-8")
    if "--background" not in sys.argv:
        webbrowser.open(path.as_uri())


@contextlib.contextmanager
def service_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "service.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"1")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    root = installed_root(mobile_state())
    root.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('codex-relay-service')
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(root / 'service.log', maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    explicit = '--background' not in sys.argv or '--enable-startup' in sys.argv
    if explicit:
        (root / 'service.paused').unlink(missing_ok=True)
        if getattr(sys, 'frozen', False):
            try:
                install(root)
            except Exception as error:
                log.exception('登录启动注册失败')
                (root / 'startup.json').write_text(json.dumps({'enabled': False, 'error': str(error)}), 'utf-8')
    elif (root / 'service.paused').exists():
        return
    config_path = root / "config.json"
    config = json.loads(config_path.read_text("utf-8")) if config_path.exists() else {}
    local = f"http://127.0.0.1:{config.get('port', 8879)}"
    try:
        if sys.platform == "darwin" and "--background" not in sys.argv:
            config = config if config.get("origin") else configure(root)
            # Finder can reopen the launcher after its foreground process exits.
            command = [sys.executable]
            if not getattr(sys, "frozen", False):
                command.append(str(pathlib.Path(__file__).resolve()))
            command.extend(['--background', '--data-dir', str(root)])
            environment = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
            subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1.5)
            webbrowser.open(local + "/connect")
            return
        with service_lock(root) as acquired:
            if not acquired:
                if "--background" not in sys.argv:
                    webbrowser.open(local + "/connect")
                return
            config = config if config.get("origin") else configure(root)
            server = HTTPServer(Relay(root, config))
            log.info('服务启动 pid=%s origin=%s data=%s', os.getpid(), config['origin'], root)
            (root / "启动说明.html").unlink(missing_ok=True)
            if "--background" not in sys.argv:
                threading.Timer(0.8, webbrowser.open, args=(local + "/connect",)).start()
            try:
                server.serve_forever(poll_interval=0.3)
            finally:
                server.server_close()
                log.info('服务正常结束 pid=%s paused=%s', os.getpid(), (root / 'service.paused').exists())
    except Exception as error:
        log.exception('服务启动或运行失败')
        error_page(root, error)
        return 1
    finally:
        log.removeHandler(handler)
        handler.close()
    return 0


if __name__ == "__main__":
    if '--app-bridge' in sys.argv:
        from app_bridge import helper_main
        helper_main()
    elif '--grok-run' in sys.argv:
        from grok_backend import run_cli
        run_cli(sys.argv[-1])
    else:
        sys.exit(main())
