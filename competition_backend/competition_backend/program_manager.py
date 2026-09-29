"""本机程序状态与 SSH 机载启动。故障隔离，不发送任何飞行或解锁指令。"""
import base64
import codecs
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

NAMES = {'ground': '竞赛程序地面端', 'onboard': '竞赛程序机载端',
         'detection': '目标检测程序', 'flight': '自主飞行指令程序'}


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[认证信息已隐藏]')
    return re.sub(r'(?i)((?:auth_token|peer_token|--token)\s*[:= ]\s*)\S+', r'\1[认证信息已隐藏]', text)


class ConsoleTee:
    def __init__(self, stream, path, secrets=()):
        self.stream, self.path, self.secrets = stream, Path(path), secrets
        self.lock = threading.Lock()
    def write(self, text):
        with self.lock:
            self.stream.write(text)
            with self.path.open('a', encoding='utf-8') as log:
                log.write(redact(text, self.secrets))
        return len(text)
    def flush(self):
        self.stream.flush()
    def isatty(self):
        return False
    @property
    def encoding(self):
        return 'utf-8'


class ProgramManager:
    def __init__(self, root, environment=None):
        self.root = Path(root)
        self.env = dict(os.environ if environment is None else environment)
        self.secrets = [value for key, value in self.env.items() if 'TOKEN' in key and value]
        self.logs = self.root / 'ground_logs' / 'programs'
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.local = None
        self.states = {key: dict(state='idle', detail='尚未启动') for key in NAMES}
        self.states['ground'] = dict(state='running', detail='地面网页服务运行中')

    def select(self, local):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return  # 同一个入口只控制当前选中的固定配对无人机。
            self.local = dict(local)
            self.logs.mkdir(parents=True, exist_ok=True)
            for key in NAMES:
                if key != 'ground':
                    self.states[key] = dict(state='starting', detail='正在连接 UAV%s' % local['uav_id'])
            self.thread = threading.Thread(target=self._watch, name='onboard-program-monitor', daemon=True)
            self.thread.start()

    def _path(self, key):
        if key == 'ground':
            return self.logs / 'ground.log'
        return self.logs / ('uav%s_%s.log' % (self.local['uav_id'] if self.local else 0, key))

    def _append(self, key, text):
        if not text:
            return
        self.logs.mkdir(parents=True, exist_ok=True)
        with self._path(key).open('a', encoding='utf-8') as stream:
            stream.write(redact(text, self.secrets))

    def _request(self, action, offsets):
        source = (self.root / 'tools' / 'remote_programs.py').read_text(encoding='utf-8-sig')
        payload = dict(action=action, uav_id=self.local['uav_id'], model=self.local['model'], offsets=offsets)
        code = 'PROGRAM_SOURCE = ' + repr(source) + '\n' + source + '\nrpc(' + repr(payload) + ')\n'
        user = self.env.get('COMPETITION_SSH_USER', 'amov')
        # accept-new 只接受初次连接，不覆盖已改变的主机密钥。
        command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'ConnectTimeout=4', '-o', 'ServerAliveInterval=4', '-o', 'ServerAliveCountMax=1',
                   '%s@%s' % (user, self.local['onboard_host']), 'python3 -']
        result = subprocess.run(command, input=code.encode(), capture_output=True, timeout=16,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            detail = (result.stderr or result.stdout).decode('utf-8', errors='replace').strip()
            raise RuntimeError('SSH 连接或机载程序管理失败：' + detail)
        return json.loads(result.stdout.decode('utf-8'))

    def _watch(self):
        cursor_path = self.logs / ('uav%s_offsets.json' % self.local['uav_id'])
        saved = {}
        try:
            record = json.loads(cursor_path.read_text(encoding='utf-8'))
            if record.get('host') == self.local.get('onboard_host'):
                saved = record.get('offsets', {})
        except (OSError, ValueError):
            pass
        offsets = {key: int(saved.get(key, 0)) if self._path(key).exists() else 0 for key in NAMES if key != 'ground'}
        decoders = {key: codecs.getincrementaldecoder('utf-8')(errors='replace') for key in offsets}
        action, failed_once = 'start', False
        while not self.stop_event.is_set():
            try:
                result = self._request(action, offsets)
                action, failed_once = 'status', False
                for key in offsets:
                    info = result[key]
                    raw = base64.b64decode(info.pop('log', ''))
                    if info.get('offset', offsets[key]) < offsets[key]:
                        decoders[key].reset()
                        self._append(key, '\n机载日志重新开始。\n')
                    self._append(key, decoders[key].decode(raw))
                    if info.get('state') == 'error' and not raw:
                        with self.lock:
                            previous = self.states[key].get('detail')
                        if previous != info.get('detail'):
                            self._append(key, '\n' + info.get('detail', '程序异常') + '\n')
                    offsets[key] = info.get('offset', offsets[key])
                    with self.lock:
                        self.states[key] = {**info, 'checked_at': time.time()}
                cursor_path.parent.mkdir(parents=True, exist_ok=True)
                cursor_path.write_text(json.dumps({'host': self.local.get('onboard_host'), 'offsets': offsets}), encoding='utf-8')
            except Exception as error:
                for key in offsets:
                    with self.lock:
                        self.states[key] = dict(state='error', detail=str(error), checked_at=time.time())
                    if not failed_once:
                        self._append(key, '\n%s\n' % error)
                failed_once = True
                # 首次连接失败不在稍后突然启动飞机上的程序；重新确认终端后才重试启动。
                if action == 'start':
                    break
            # 后续仅查询进程和增量日志，进程崩溃不自动重启，也不影响飞行/任务链路。
            if self.stop_event.wait(1):
                break

    def snapshot(self):
        with self.lock:
            return {'uav_id': self.local['uav_id'] if self.local else None,
                    'programs': {key: dict(name=NAMES[key], **state) for key, state in self.states.items()}}

    def read_log(self, key, offset=0, limit=65536):
        if key not in NAMES:
            raise ValueError('程序名称不存在')
        path = self._path(key)
        size = path.stat().st_size if path.exists() else 0
        offset = max(0, min(int(offset), size))
        raw = b''
        if size:
            with path.open('rb') as stream:
                stream.seek(offset)
                raw = stream.read(max(1, min(int(limit), 65536)))
                # 保证分页处不拆开 UTF-8 汉字。
                if offset + len(raw) < size:
                    for extra in range(4):
                        try:
                            raw.decode('utf-8')
                            break
                        except UnicodeDecodeError as error:
                            if error.reason != 'unexpected end of data':
                                break
                            raw += stream.read(1)
        return dict(text=redact(raw.decode('utf-8', errors='replace'), self.secrets),
                    offset=offset + len(raw), size=size)

    def stop(self):
        # 关闭网页后端不终止机载竞赛、检测或程序 B。
        self.stop_event.set()
