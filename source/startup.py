from __future__ import annotations

import csv
import io
import json
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


TASK_NAME = 'CodexRelayMobile'


def installed_root(root):
    """Keep background launches in the same physical data directory as pairing."""
    root = pathlib.Path(root).resolve()
    if os.name == 'nt' and (root / 'config.json').exists():
        import ctypes
        import msvcrt
        function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint]
        buffer = ctypes.create_unicode_buffer(32768)
        with (root / 'config.json').open('rb') as handle:
            length = function(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise OSError('无法确定连接数据目录。')
        value = buffer.value
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        root = pathlib.Path(value).parent
    return root


def windows_task(executable, root, user):
    namespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', namespace)
    task = ET.Element('Task', {'version': '1.2', 'xmlns': namespace})
    def item(parent, name, value=None):
        result = ET.SubElement(parent, name)
        if value is not None:
            result.text = value
        return result
    triggers = item(task, 'Triggers')
    logon = item(triggers, 'LogonTrigger')
    item(logon, 'Enabled', 'true')
    item(logon, 'UserId', user)
    timer = item(triggers, 'TimeTrigger')
    repeat = item(timer, 'Repetition')
    item(repeat, 'Interval', 'PT1M')
    item(repeat, 'StopAtDurationEnd', 'false')
    item(timer, 'StartBoundary', '2020-01-01T00:00:00')
    item(timer, 'Enabled', 'true')
    principal = item(item(task, 'Principals'), 'Principal')
    principal.set('id', 'User')
    item(principal, 'UserId', user)
    item(principal, 'LogonType', 'InteractiveToken')
    item(principal, 'RunLevel', 'LeastPrivilege')
    settings = item(task, 'Settings')
    for name, value in [('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                        ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'true'),
                        ('ExecutionTimeLimit', 'PT0S'), ('Enabled', 'true')]:
        item(settings, name, value)
    actions = item(task, 'Actions')
    actions.set('Context', 'User')
    execute = item(actions, 'Exec')
    item(execute, 'Command', str(executable))
    item(execute, 'Arguments', subprocess.list2cmdline(['--background', '--data-dir', str(root)]))
    item(execute, 'WorkingDirectory', str(pathlib.Path(executable).parent))
    return ET.tostring(task, encoding='unicode')


def install(root):
    executable = pathlib.Path(sys.executable).resolve()
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('自动恢复需要使用打包后的应用。')
    if sys.platform == 'darwin':
        agent = pathlib.Path.home() / 'Library/LaunchAgents/local.codexrelay.mobile.plist'
        agent.parent.mkdir(parents=True, exist_ok=True)
        job = {'Label': 'local.codexrelay.mobile',
               'ProgramArguments': [str(executable), '--background', '--data-dir', str(root)],
               'RunAtLoad': True, 'StartInterval': 30, 'ProcessType': 'Background', 'ThrottleInterval': 15}
        with agent.open('wb') as handle:
            plistlib.dump(job, handle)
        domain = f'gui/{os.getuid()}'
        subprocess.run(['/bin/launchctl', 'bootout', domain, str(agent)], capture_output=True, timeout=15)
        result = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(agent)],
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or '无法注册登录启动。')
    elif os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                                text=True, check=True, creationflags=flags)
        user = next(csv.reader(io.StringIO(result.stdout.strip())))[1]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'task.xml'
            path.write_text(windows_task(executable, root, user), encoding='utf-16')
            result = subprocess.run(['schtasks.exe', '/Create', '/TN', TASK_NAME, '/XML', str(path), '/F'],
                                    capture_output=True, text=True, creationflags=flags, timeout=20)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or '无法注册登录启动。')
    else:
        raise RuntimeError('此平台尚不支持自动恢复。')
    (root / 'startup.json').write_text(json.dumps({'enabled': True, 'executable': str(executable),
        'data_dir': str(root), 'recovery_seconds': 30 if sys.platform == 'darwin' else 60}), 'utf-8')
