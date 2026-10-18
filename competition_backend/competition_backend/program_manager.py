"""本机程序状态与 SSH 机载启动。故障隔离，不发送任何飞行或解锁指令。"""
import base64
import errno
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import zlib

from .ssh_identity import ssh_identity_args
from .program_logs import LatestProgramLog
from .prometheus_connection import PrometheusConnection

NAMES = {'ground': '竞赛程序地面端', 'onboard': '竞赛程序机载端',
         'detection': '车辆工事检测程序', 'detection_person': '人员检测程序',
         'flight': '自主飞行指令程序', 'drop': '抛投程序'}


class SSHTransportError(RuntimeError):
    """请求是否执行未知；恢复后先查询机载记录，不能据此禁止自动启动。"""


def transient_connection_error(error):
    if isinstance(error, (SSHTransportError, TimeoutError, ConnectionError, subprocess.TimeoutExpired)):
        return True
    if isinstance(error, OSError) and error.errno in (
            errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNRESET,
            errno.ECONNREFUSED, errno.EPIPE, errno.ETIMEDOUT):
        return True
    return bool(re.search(
        r'(?i)\btimeout\b|timed out|server .+ not responding|connection (?:reset|closed|refused|aborted)'
        r'|broken pipe|no route to host|network is (?:unreachable|down)|connection lost', str(error)))


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
        self.connection = None
        self.connection_checked = 0.0
        self.connection_status = {}
        self.connection_audit_signature = ''
        self._ssh_bind_unsupported = False
        self._ssh_bind_notice_written = False
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

    def _check_default_ssh_route(self):
        """旧版 Windows SSH 兼容重试前核对出口；UDP connect 不发送数据报。"""
        expected = self.local['ground_host']
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(2)
                probe.connect((self.local['onboard_host'], 22))
                source = probe.getsockname()[0]
        except OSError as error:
            raise SSHTransportError('旧版 Windows SSH 地址绑定不兼容，默认出口核验失败：%s' % error) from error
        if source != expected:
            raise SSHTransportError(
                '旧版 Windows SSH 地址绑定不兼容，默认出口 %s 与图传地址 %s 不一致；'
                '未发送兼容重试，请检查图传网卡及路由' % (source, expected))

    def _run_ssh(self, command, **kwargs):
        """仅在连接建立前的已知 -b 错误上重试；其他失败保持原恢复流程。"""
        binding_index = command.index('-b') if '-b' in command else None
        compatible = sys.platform == 'win32' and binding_index is not None
        if not (compatible and self._ssh_bind_unsupported):
            result = subprocess.run(command, **kwargs)
            # 精确匹配 OpenSSH 本地 connect 错误，不能因远端脚本报错而重复执行请求。
            if not (compatible and result.returncode == 255 and not result.stdout.strip()
                    and re.fullmatch(r'ssh: connect to host %s port 22: Invalid argument'
                                     % re.escape(self.local['onboard_host']),
                                     result.stderr.decode('utf-8', errors='replace').strip(), re.IGNORECASE)):
                return result
            self._ssh_bind_unsupported = True
        # 每次兼容连接都重新核对，不能把先前成功当作网卡/路由永不变化。
        self._check_default_ssh_route()
        if not self._ssh_bind_notice_written:
            try:
                self._append('ground', '[%s] SSH 兼容模式：检测到 Windows SSH 的 -b 地址绑定错误；'
                             '已核验默认出口为 %s，后续每次连接核验后省略 -b，保留原密钥和主机校验。\n'
                             % (time.strftime('%Y-%m-%d %H:%M:%S'), self.local['ground_host']))
                self._ssh_bind_notice_written = True
            except OSError:
                pass  # 诊断写入失败不能改变连接结果。
        return subprocess.run(command[:binding_index] + command[binding_index + 2:], **kwargs)

    def _request(self, action, offsets, program=None):
        if action in ('prometheus_probe', 'prometheus_gps'):
            source = (self.root / 'tools/remote_prometheus.py').read_text(encoding='utf-8-sig')
        elif action == 'clock_probe':
            # 旧版节点的只读兼容探针只需数百字节，不再传输整个程序管理器。
            source = ("import json, time\nfrom pathlib import Path\n"
                      "def rpc(payload):\n"
                      "    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()\n"
                      "    print(json.dumps(dict(boot_id=boot, remote_monotonic=time.monotonic(), remote_wall=time.time())))\n")
        else:
            source = (self.root / 'tools' / 'remote_programs.py').read_text(encoding='utf-8-sig')
        payload = dict(action=action, uav_id=self.local['uav_id'], model=self.local['model'], offsets=offsets,
                       program=program, latest_log=True, log_limit=8192 if action == 'status' else 0)
        if action == 'prometheus_gps':
            payload['expected_boot_id'] = program
        if action == 'auto_start':
            payload['expected_boot_id'] = self.boot_id
            payload['skip_programs'] = sorted(self.operator_stopped)
        if action in ('start', 'auto_start'):
            config_path = self.root / 'config' / 'onboard_programs.json'
            if config_path.exists():
                try:
                    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
                    commands = config.get('commands') if isinstance(config, dict) else None
                    if not isinstance(commands, dict) or set(commands) - (set(NAMES) - {'ground'}):
                        raise ValueError('commands 必须为仅包含 onboard、detection、detection_person、flight、drop 的对象')
                    payload['commands'] = commands
                except (OSError, ValueError) as error:
                    raise RuntimeError('读取机载启动配置 config/onboard_programs.json 失败：%s' % error) from error
        # Windows OpenSSH/远端终端的编码设置可能不同。stdin 只传 ASCII，
        # 由远端 Python 明确按 UTF-8 还原含中文的管理脚本。
        source_b64 = base64.b64encode(zlib.compress(source.encode('utf-8'))).decode('ascii')
        payload_json = json.dumps(payload, ensure_ascii=True, separators=(',', ':'))
        code = ("import base64, json, zlib, io, contextlib\n"
                f"PROGRAM_SOURCE = zlib.decompress(base64.b64decode('{source_b64}')).decode('utf-8')\n"
                "exec(compile(PROGRAM_SOURCE, %r, 'exec'))\n" % (
                    'remote_prometheus.py' if action.startswith('prometheus_') else 'remote_programs.py') +
                "_rpc_output = io.StringIO()\n"
                "with contextlib.redirect_stdout(_rpc_output):\n"
                f"    rpc(json.loads({payload_json!r}))\n"
                "print('ZHX1:' + base64.b64encode(zlib.compress(_rpc_output.getvalue().encode('utf-8'))).decode('ascii'))\n")
        user = self.env.get('COMPETITION_SSH_USER', 'amov')
        # accept-new 只接受初次连接，不覆盖已改变的主机密钥。
        binding = ['-b', self.local['ground_host']] if self.local.get('ground_host') else []
        command = ['ssh', *ssh_identity_args(), *binding, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                   '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3',
                   '%s@%s' % (user, self.local['onboard_host']), 'python3 -']
        if action in ('prometheus_probe', 'prometheus_gps'):
            command[-1] = "bash -c 'source /opt/ros/noetic/setup.bash && source ~/p600_experiment/devel/setup.bash && exec python3 -'"
        started = time.monotonic()
        try:
            result = self._run_ssh(command, input=code.encode('ascii'), capture_output=True, timeout=4 if action == 'clock_probe' else 45,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                detail = (result.stderr or result.stdout).decode('utf-8', errors='replace').strip()
                # OpenSSH 255 是连接失败；认证/主机密钥错误仍需明确处理，不伪装成弱网。
                authentication_error = re.search(r'(?i)permission denied|host key verification failed|'
                                                 r'remote host identification has changed|bad permissions', detail)
                if (result.returncode == 255 and not authentication_error) or transient_connection_error(RuntimeError(detail)):
                    raise SSHTransportError('SSH 连接暂时中断：' + detail)
                raise RuntimeError('SSH 连接或机载程序管理失败：' + detail)
            try:
                response = result.stdout.strip()
                if response.startswith(b'ZHX1:'):
                    response = zlib.decompress(base64.b64decode(response[5:], validate=True))
                answer = json.loads(response.decode('utf-8'))
            except (ValueError, UnicodeError, zlib.error) as error:
                raise SSHTransportError('SSH 响应不完整，将重新查询机载实际状态') from error
        except Exception as error:
            detail = str(error)
            stage = ('ros_master' if '[ros_master]' in detail else
                     'ssh_authentication' if 'Permission denied' in detail else
                     'ssh_transport' if isinstance(error, SSHTransportError) else
                     'ssh_request_timeout' if isinstance(error, subprocess.TimeoutExpired) else 'remote_execution')
            self._request_diagnostic(action, program, stage, started, detail)
            raise
        self._request_diagnostic(action, program, 'completed', started, '')
        return answer

    def _request_diagnostic(self, action, program, stage, started, error):
        try:
            audit = getattr(self, 'audit', None)
            if audit:
                audit.record_snapshot('机载管理请求诊断', (action, program), (stage, error), interval=30,
                    uav_id=self.local['uav_id'], onboard_host=self.local['onboard_host'], port=22,
                    action=action, program=program, stage=stage, boot_id=self.boot_id,
                    elapsed_seconds=round(time.monotonic()-started, 3), error=redact(error, self.secrets))
        except Exception:
            pass  # 诊断不得改变原请求的结果、重试和授权逻辑。

    def _prepare_connection(self, force=False):
        # P600 使用实机核验过的厂商连接；独立程序管理器未配置机队时保持原功能。
        if self.local.get('model') != 'p600' or (not (self.root / 'tools/remote_prometheus.py').exists()
                and not (self.root / 'config/onboard_programs.json').exists()):
            return dict(ready=True, auto_start_allowed=True)
        if not force and time.monotonic() - self.connection_checked < 5:
            return self.connection_status
        if self.connection is None:
            self.connection = PrometheusConnection(self.local)
        probe = None
        try:
            config_path = self.root / 'config/onboard_programs.json'
            config = json.loads(config_path.read_text(encoding='utf-8-sig')) if config_path.exists() else {}
            if config.get('prometheus_connection', {}).get('enabled', True) is False:
                self.connection_status = dict(ready=True, auto_start_allowed=True, detail='厂商自动连接已关闭')
                return self.connection_status
            probe = self._request('prometheus_probe', {})
            if self.stop_event.is_set():
                return dict(ready=False, auto_start_allowed=False, detail='地面端正在关闭')
            status = self.connection.step(probe, lambda boot: self._request('prometheus_gps', {}, program=boot))
        except Exception as error:
            transient = transient_connection_error(error)
            status = dict(ready=False, auto_start_allowed=False, state='starting' if transient else 'error',
                          detail=('连接暂时中断，正在自动重试：' if transient else '') + str(error))
        previous = self.connection_status.get('detail')
        self.connection_status = status
        self.connection_checked = time.monotonic()
        try:
            audit = getattr(self, 'audit', None)
            if audit:
                flight = (probe or {}).get('flight', {})
                health = {key: flight.get(key) for key in ('connected', 'armed', 'mode', 'location_source', 'odom_valid', 'gps_status')}
                health.update(ready=status.get('ready'), detail=status.get('detail'), boot_id=(probe or {}).get('boot_id'))
                audit.record_snapshot('基础连接分层诊断', self.local['uav_id'], health, interval=10,
                    uav_id=self.local['uav_id'], onboard_host=self.local['onboard_host'],
                    ground_host=self.local.get('ground_host'), boot_id=(probe or {}).get('boot_id'),
                    probe=probe, heartbeat=self.connection.heartbeat_snapshot(),
                    preparation_ready=status.get('ready'), detail=status.get('detail'),
                    readiness_scope='基础配置准备通过不等于GPS定位有效或起降点已记录')
        except Exception:
            pass
        preparation = status.get('preparation_audit')
        if isinstance(preparation, dict) and preparation:
            signature = json.dumps(preparation, ensure_ascii=False, sort_keys=True)
            if signature != self.connection_audit_signature:
                self.logs.mkdir(parents=True, exist_ok=True)
                entry = dict(ground_recorded_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                             uav_id=self.local['uav_id'], **preparation)
                path = self.logs / ('uav%s_connection_parameters.jsonl' % self.local['uav_id'])
                with path.open('a', encoding='utf-8') as log:
                    log.write(json.dumps(entry, ensure_ascii=False) + '\n')
                changes = preparation.get('parameter_changes')
                notice = '[%s] GPS 准备参数审计：方式=%s；准备前后差异=%s；详情=%s\n' % (
                    entry['ground_recorded_at'], preparation.get('mode'),
                    json.dumps(changes, ensure_ascii=False), path.name)
                self._append('ground', notice)
                self._append('onboard', notice)
                if getattr(self, 'audit', None):
                    self.audit.record('厂商GPS准备参数核验', uav_id=self.local['uav_id'], preparation=preparation)
                self.connection_audit_signature = signature
        if previous != status.get('detail'):
            notice = '[%s] 基础连接：%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), status.get('detail', ''))
            self._append('ground', notice)
            self._append('onboard', notice)
            if getattr(self, 'audit', None):
                self.audit.record('厂商基础连接准备', uav_id=self.local['uav_id'], **status)
        return status

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
                if action == 'start':
                    connection = self._prepare_connection(force=True)
                    if not connection.get('ready'):
                        raise RuntimeError(connection.get('detail', '厂商基础连接尚未就绪'))
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
        retry_delay = 1
        while not self.stop_event.is_set():
            if self.restart_request.is_set():
                self.restart_request.clear()
                action, authorization_attempted = 'start', False
            try:
                with self.request_lock:
                    connection = self._prepare_connection(force=action in ('start', 'auto_start'))
                    if action in ('start', 'auto_start') and not connection.get('ready'):
                        action = 'status'
                    if action == 'auto_start' and not connection.get('auto_start_allowed', True):
                        action = 'status'
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
                            self.connection_checked = 0.0
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
                    if action == 'auto_start' and getattr(self, 'audit', None):
                        self.audit.record('机载开机自动启动结果', uav_id=self.local['uav_id'], boot_id=current_boot,
                                          reason=lifecycle.get('auto_start_reason'),
                                          states={key: result[key].get('state') for key in offsets})
                    action, failed_once = 'status', False
                    retry_delay = 1
                    if (current_boot and pending and self.auto_blocked_boot != current_boot
                            and connection.get('ready') and connection.get('auto_start_allowed', True)):
                        action = 'auto_start'
                        if getattr(self, 'audit', None):
                            self.audit.record('机载开机自动启动请求', uav_id=self.local['uav_id'],
                                              boot_id=current_boot, programs=pending)
                    for key in offsets:
                        info = result[key]
                        if not connection.get('ready') and info.get('state') not in ('running', 'stopped'):
                            info = dict(info, state=connection.get('state', 'starting'),
                                        detail='基础连接准备：' + connection.get('detail', '等待连接'))
                        with self.lock:
                            mirror = self._mirror(key)
                            try:
                                mirror.consume(info, boot_id=info.get('log_boot_id', ''))
                            except OSError as error:
                                # 磁盘或归档失败不能把运行中的程序判为停止，也不能截断旧证据。
                                if getattr(self, 'audit', None):
                                    self.audit.record_snapshot('程序打印保存失败', key, str(error), interval=30,
                                        program=key, uav_id=self.local['uav_id'], error=str(error))
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
                transient = transient_connection_error(error)
                retry_delay = min(retry_delay * 2, 8) if transient else 1
                for key in offsets:
                    with self.lock:
                        waiting = transient
                        self.states[key] = dict(state='starting' if waiting else 'error',
                                                detail=('连接暂时中断，持续自动重试并核对启动进度：' if waiting else '') + str(error),
                                                checked_at=time.time())
                    if not failed_once:
                        self._append(key, '\n%s\n' % error)
                failed_once = True
                # 启动请求的响应可能在断网时丢失：继续查询，不能永久退出监测。
                # 查询不会重启飞机上的程序，也保留人工停止状态。
                if action in ('start', 'auto_start') and not transient:
                    self.auto_blocked_boot = self.boot_id
                action = 'status'
            # 弱网恢复后先查询，再按机载逐项记录继续本次开机尚未执行的启动。
            if self.stop_event.wait(retry_delay):
                break

    def snapshot(self):
        with self.lock:
            return {'uav_id': self.local['uav_id'] if self.local else None,
                    'boot_id': self.boot_id,
                    'authorization': dict(self.auth),
                    'connection': dict(self.connection_status, heartbeat_status=(
                        self.connection.heartbeat_snapshot() if self.connection else None)),
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
        # 关闭网页后端只停止地面监控，不终止五个机载程序。
        self.stop_event.set()
        if self.connection:
            self.connection.close()
