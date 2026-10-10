"""本机程序状态与 SSH 机载启动。故障隔离，不发送任何飞行或解锁指令。"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from .ssh_identity import ssh_identity_args
from .program_logs import LatestProgramLog

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
        self.restart_request = threading.Event()
        self.request_lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.operator_stopped = set()
        self.log_mirrors = {}
        self.thread = None
        self.local = None
        self.boot_id = ''
        self.auto_blocked_boot = ''
        self.auth = dict(state='idle', detail='')
        self.states = {key: dict(state='idle', detail='尚未启动') for key in NAMES}
        self.states['ground'] = dict(state='running', detail='地面网页服务运行中')

    def select(self, local):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return  # 同一个入口只控制当前选中的固定配对无人机。
            self.local = dict(local)
            self.auth = dict(state='idle', detail='')
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

    def _mirror(self, key):
        source, path = (self.local or {}).get('onboard_host', ''), self._path(key)
        existing = self.log_mirrors.get(key)
        if existing is None or existing.path != path or existing.source != source:
            self.log_mirrors[key] = LatestProgramLog(path, source, lambda text: redact(text, self.secrets))
        return self.log_mirrors[key]

    def _append(self, key, text):
        if not text:
            return
        with self.lock:
            if key != 'ground':
                self._mirror(key).append_notice(text)
            else:
                self.logs.mkdir(parents=True, exist_ok=True)
                with self._path(key).open('a', encoding='utf-8') as stream:
                    stream.write(redact(text, self.secrets))

    def _request(self, action, offsets, program=None):
        source = (self.root / 'tools' / 'remote_programs.py').read_text(encoding='utf-8-sig')
        payload = dict(action=action, uav_id=self.local['uav_id'], model=self.local['model'], offsets=offsets, program=program, latest_log=True)
        if action == 'auto_start':
            payload['expected_boot_id'] = self.boot_id
            payload['skip_programs'] = sorted(self.operator_stopped)
        if action in ('start', 'auto_start'):
            config_path = self.root / 'config' / 'onboard_programs.json'
            if config_path.exists():
                try:
                    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
                    commands = config.get('commands') if isinstance(config, dict) else None
                    if not isinstance(commands, dict) or set(commands) - {'onboard', 'detection', 'flight'}:
                        raise ValueError('commands 必须为仅包含 onboard、detection、flight 的对象')
                    payload['commands'] = commands
                except (OSError, ValueError) as error:
                    raise RuntimeError('读取机载启动配置 config/onboard_programs.json 失败：%s' % error) from error
        # Windows OpenSSH/远端终端的编码设置可能不同。stdin 只传 ASCII，
        # 由远端 Python 明确按 UTF-8 还原含中文的管理脚本。
        source_b64 = base64.b64encode(source.encode('utf-8')).decode('ascii')
        payload_json = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
        code = ("import base64, json\n"
                f"PROGRAM_SOURCE = base64.b64decode('{source_b64}').decode('utf-8')\n"
                "exec(compile(PROGRAM_SOURCE, 'remote_programs.py', 'exec'))\n"
                f"rpc(json.loads({payload_json!r}))\n")
        user = self.env.get('COMPETITION_SSH_USER', 'amov')
        # accept-new 只接受初次连接，不覆盖已改变的主机密钥。
        command = ['ssh', *ssh_identity_args(), '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'ConnectTimeout=4', '-o', 'ServerAliveInterval=4', '-o', 'ServerAliveCountMax=1',
                   '%s@%s' % (user, self.local['onboard_host']), 'python3 -']
        result = subprocess.run(command, input=code.encode('ascii'), capture_output=True, timeout=25 if action == 'stop' else (4 if action == 'clock_probe' else 16),
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            detail = (result.stderr or result.stdout).decode('utf-8', errors='replace').strip()
            raise RuntimeError('SSH 连接或机载程序管理失败：' + detail)
        return json.loads(result.stdout.decode('utf-8'))

    def start_program(self, key):
        """显式启动一个机载程序；已运行则复用，不触发整批重连启动。"""
        return self._operate_program(key, 'start')

    def stop_program(self, key):
        return self._operate_program(key, 'stop')

    def _operate_program(self, key, action):
        if key not in NAMES or key == 'ground':
            raise ValueError('机载程序名称无效')
        if not self.local:
            raise ValueError('请先选择本地地面终端')
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('另一程序操作正在执行，请等待其退出核验或启动结果')
        verb = '启动' if action == 'start' else '停止'
        try:
            # 不无限等待 SSH 监测；同一管理器的启动、停止串行执行。
            if not self.request_lock.acquire(timeout=20):
                raise RuntimeError('SSH 管理连接忙，本次未发送%s指令' % verb)
            try:
                with self.lock:
                    if action == 'stop':
                        self.operator_stopped.add(key)
                    else:
                        self.operator_stopped.discard(key)
                    self.states[key] = dict(state='starting' if action == 'start' else 'stopping',
                                            detail='正在%s%s' % (verb, NAMES[key]))
                self._append(key, '\n操作员请求%s%s。\n' % (verb, NAMES[key]))
                if getattr(self, 'audit', None):
                    self.audit.record('机载程序人工操作', action=action, program=key, uav_id=self.local['uav_id'])
                result = self._request(action, {}, program=key)[key]
                # 日志由监测线程按原偏移增量读取，避免重复追加完整历史。
                result.pop('log', None)
                if action == 'stop' and not result.get('stop_verified'):
                    raise RuntimeError(result.get('detail', '尚未确认进程全部退出'))
                if action == 'start' and result.get('state') not in ('starting', 'running'):
                    raise RuntimeError(result.get('detail', '启动尚未确认'))
                with self.lock:
                    self.states[key] = dict(result, checked_at=time.time())
                self._append(key, result.get('detail', '') + '\n')
                if getattr(self, 'audit', None):
                    self.audit.record('机载程序人工操作结果', action=action, program=key,
                                      uav_id=self.local['uav_id'], result=result)
                return result
            finally:
                self.request_lock.release()
        except Exception as error:
            detail = verb + '未确认：' + str(error)
            with self.lock:
                self.states[key] = dict(state='error', detail=detail, stop_verified=False)
            self._append(key, detail + '\n')
            if getattr(self, 'audit', None):
                self.audit.record('机载程序人工操作失败', action=action, program=key,
                                  uav_id=self.local['uav_id'], error=str(error))
            raise RuntimeError(detail) from error
        finally:
            self.operation_lock.release()

    def _authorize(self):
        if os.name != 'nt':
            raise RuntimeError('请先配置 SSH 公钥登录，再点击重新连接并启动')
        with self.lock:
            self.auth = dict(state='waiting', detail='请在弹出的 SSH 授权窗口输入机载 Ubuntu 密码；完成后自动继续启动。')
            for key in NAMES:
                if key != 'ground':
                    self.states[key] = dict(state='auth_required', detail=self.auth['detail'])
        # 密码只交给 Windows OpenSSH 的交互终端；不进入网页、Python 参数或日志。
        process = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
             str(self.root / 'tools/ssh_authorize_window.ps1'), '-UavAddress', self.local['onboard_host'],
             '-UavUser', self.env.get('COMPETITION_SSH_USER', 'amov')],
            cwd=str(self.root), creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0),
        )
        while process.poll() is None:
            if self.stop_event.wait(.25):
                return False
        if process.returncode != 0:
            raise RuntimeError('SSH 授权未完成或窗口已关闭，请点击重新连接并启动后重试')
        with self.lock:
            self.auth = dict(state='ready', detail='SSH 授权完成，正在继续启动')
        return True

    def reconnect(self):
        if not self.local:
            raise ValueError('请先选择本地地面终端')
        with self.lock:
            # 主动点击重新连接才恢复启动；状态轮询仍不重启人工停止的程序。
            self.operator_stopped.clear()
            self.auto_blocked_boot = ''
            if self.thread and self.thread.is_alive():
                if self.auth['state'] != 'waiting':
                    self.restart_request.set()
            else:
                self.restart_request.set()
                self.select(self.local)
        return self.snapshot()

    def _watch(self):
        # 每个程序独立保存本次日志会话、字节偏移和 UTF-8 解码状态。
        with self.lock:
            offsets = {key: self._mirror(key).offset for key in NAMES if key != 'ground'}
        # 先读本次开机标识与机载持久记录。不能把首次 SSH 失败或链路恢复当作启动依据。
        action, failed_once, authorization_attempted = 'status', False, False
        while not self.stop_event.is_set():
            if self.restart_request.is_set():
                self.restart_request.clear()
                action, authorization_attempted = 'start', False
            try:
                with self.request_lock:
                    if action == 'start' and self.operator_stopped:
                        result = self._request('status', offsets)
                        for key in NAMES:
                            if key != 'ground' and key not in self.operator_stopped:
                                result[key] = self._request('start', offsets, program=key)[key]
                    else:
                        result = self._request(action, offsets)
                    lifecycle = result.get('_lifecycle', {})
                    current_boot = lifecycle.get('boot_id', '')
                    if current_boot and current_boot != self.boot_id:
                        previous_boot = self.boot_id
                        with self.lock:
                            self.boot_id = current_boot
                            self.auto_blocked_boot = ''
                            if previous_boot:
                                self.operator_stopped.clear()
                        description = '检测到机载重新开机' if previous_boot else '已识别机载本次开机'
                        if getattr(self, 'audit', None):
                            self.audit.record(description, uav_id=self.local['uav_id'],
                                              boot_id=current_boot, previous_boot_id=previous_boot)
                        for key in offsets:
                            self._append(key, '\n%s，开机标识：%s。\n' % (description, current_boot))
                            if lifecycle.get('auto_start_reason') == 'existing_session':
                                self._append(key, '检测到已有业务程序，本次只接管状态与日志监控；缺失程序需操作员主动启动。\n')
                    if failed_once:
                        if getattr(self, 'audit', None):
                            self.audit.record('机载程序管理连接恢复', uav_id=self.local['uav_id'], boot_id=current_boot)
                        for key in offsets:
                            self._append(key, '\nSSH 已恢复，继续读取原程序状态与日志。\n')
                    pending = [key for key in lifecycle.get('auto_start_pending', [])
                               if key in offsets and key not in self.operator_stopped]
                    if action == 'auto_start' and pending:
                        # 标记无法落盘等管理错误需要人工处理；继续监控，不无限尝试启动。
                        self.auto_blocked_boot = current_boot
                    if action == 'auto_start' and getattr(self, 'audit', None):
                        self.audit.record('机载开机自动启动结果', uav_id=self.local['uav_id'], boot_id=current_boot,
                                          reason=lifecycle.get('auto_start_reason'),
                                          states={key: result[key].get('state') for key in offsets})
                    action, failed_once = 'status', False
                    if current_boot and pending and self.auto_blocked_boot != current_boot:
                        action = 'auto_start'
                        if getattr(self, 'audit', None):
                            self.audit.record('机载开机自动启动请求', uav_id=self.local['uav_id'],
                                              boot_id=current_boot, programs=pending)
                    for key in offsets:
                        info = result[key]
                        with self.lock:
                            mirror = self._mirror(key)
                            mirror.consume(info)
                            offsets[key] = mirror.offset
                        raw = info.pop('log', '')
                        if info.get('state') == 'error' and not raw:
                            with self.lock:
                                previous = self.states[key].get('detail')
                            if previous != info.get('detail'):
                                self._append(key, '\n' + info.get('detail', '程序异常') + '\n')
                        with self.lock:
                            if getattr(self, 'audit', None) and any(self.states[key].get(k) != info.get(k) for k in ('state', 'detail', 'pid')):
                                self.audit.record('机载程序状态变化', program=NAMES[key], uav_id=self.local['uav_id'],
                                                  state=info.get('state'), detail=info.get('detail'), pid=info.get('pid'))
                            self.states[key] = {**info, 'checked_at': time.time()}
            except Exception as error:
                if not failed_once and getattr(self, 'audit', None):
                    self.audit.record('机载程序管理失败', uav_id=self.local['uav_id'], error=str(error))
                if not authorization_attempted and re.search(r'Permission denied.*(?:publickey|password)', str(error)):
                    authorization_attempted = True
                    try:
                        if self._authorize():
                            continue
                        break
                    except Exception as auth_error:
                        error = auth_error
                        with self.lock:
                            self.auth = dict(state='error', detail=str(error))
                transient = isinstance(error, (OSError, subprocess.TimeoutExpired)) or re.search(
                    r'(?i)timed out|connection (?:reset|closed|refused)|broken pipe|no route to host|network is unreachable',
                    str(error))
                for key in offsets:
                    with self.lock:
                        waiting = transient and not self.boot_id
                        self.states[key] = dict(state='starting' if waiting else 'error',
                                                detail=('等待无人机开机及 SSH 就绪：' if waiting else '') + str(error),
                                                checked_at=time.time())
                    if not failed_once:
                        self._append(key, '\n%s\n' % error)
                failed_once = True
                # 启动请求的响应可能在断网时丢失：继续查询，不能永久退出监测。
                # 查询不会重启飞机上的程序，也保留人工停止状态。
                if action in ('start', 'auto_start') and not transient:
                    self.auto_blocked_boot = self.boot_id
                action = 'status'
            # 同次开机只有只读查询；新开机才允许发起一批自动启动。
            if self.stop_event.wait(1):
                break

    def snapshot(self):
        with self.lock:
            return {'uav_id': self.local['uav_id'] if self.local else None,
                    'boot_id': self.boot_id,
                    'authorization': dict(self.auth),
                    'programs': {key: dict(name=NAMES[key], **state) for key, state in self.states.items()}}

    def read_log(self, key, offset=0, limit=65536, generation=''):
        if key not in NAMES:
            raise ValueError('程序名称不存在')
        with self.lock:
            current = self._mirror(key).generation if key != 'ground' else ''
            reset = bool(generation and generation != current)
            path = self._path(key)
            size = path.stat().st_size if path.exists() else 0
            offset = 0 if reset else max(0, min(int(offset), size))
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
                        offset=offset + len(raw), size=size, generation=current, reset=reset)

    def stop(self):
        # 关闭网页后端不终止机载竞赛、检测或程序 B。
        self.stop_event.set()
