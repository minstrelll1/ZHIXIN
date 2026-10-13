"""One standalone task TCP listener per paired ground computer."""
from __future__ import annotations

import copy
import hmac
import json
import math
import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common import (FAILED_PHASES, FIXED_SHA256, STARTED_PHASES, Failure, canonical,
                    load_fixed, read_json, require, save_json, validate_fixed_assignment)
from competition_shared.fleet import validate_identity
from frozen_link.tcp_adapter import TcpFleetAdapter


class Audit:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_error = ''

    def record(self, label, **details):
        status = details.get('status') or {}
        error = status.get('error') or details.get('error')
        if error:
            with self.lock:
                self.last_error = str(error)
            print('{}: {}'.format(label, error), flush=True)


def check_program_b(vehicle, config):
    """Read ROS registrations over the existing SSH key; never launch any process."""
    uid = vehicle['uav_id']
    source = '''import json, socket, xmlrpc.client
socket.setdefaulttimeout(3)
master = xmlrpc.client.ServerProxy('http://127.0.0.1:11311')
code, text, state = master.getSystemState('/emergency_readonly_check')
if code != 1: raise RuntimeError(text)
subs = dict(state[1])
prefix = '/ground_mission_planner/vehicle_%d/' % UID
topics = [prefix + key for key in ('path_stage_1', 'jiangluodian', 'entry_path', 'return_paths')]
topics.append('/ground_mission_planner/recon_start_mode')
live = {}
for topic in topics:
    live[topic] = []
    for node in subs.get(topic, []):
        try:
            code, text, uri = master.lookupNode('/emergency_readonly_check', node)
            if code == 1 and xmlrpc.client.ServerProxy(uri).getPid('/emergency_readonly_check')[0] == 1:
                live[topic].append(node)
        except Exception: pass
common = set(live[topics[0]]) & set(live[topics[1]]) & set(live[topics[-1]])
print(json.dumps(dict(ready=bool(common) and all(live.values()), subscribers=live)))
'''
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4',
               '-o', 'StrictHostKeyChecking=yes']
    key = Path(config['ssh_key']).expanduser()
    if key.is_file():
        command += ['-i', str(key), '-o', 'IdentitiesOnly=yes']
    command += ['{}@{}'.format(config['ssh_user'], vehicle['onboard_host']), 'python3 -']
    try:
        result = subprocess.run(command, input='UID={}\n'.format(uid) + source,
                                text=True, encoding='utf-8', capture_output=True, timeout=12)
        require(result.returncode == 0, '程序 B 只读检查失败：' + result.stderr.strip()[-1200:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        require(report.get('ready') is True,
                '程序 B 航点/降落点/进场/返航/启动话题订阅不完整：' + json.dumps(report, ensure_ascii=False))
        return report
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as error:
        raise Failure('程序 B SSH/ROS 只读检查失败：{}'.format(error)) from error


class Relay:
    def __init__(self, uid, config, fleet, token, tcp_token, state_dir,
                 bind_host='0.0.0.0', tcp_port=None):
        self.uid, self.config, self.fleet, self.token = uid, config, fleet, token
        self.vehicle = next(v for v in fleet['vehicles'] if v['uav_id'] == uid)
        self.path = Path(state_dir) / 'relay-uav{}.json'.format(uid)
        self.state_lock = threading.RLock()
        self.control_lock = threading.Lock()
        self.prepared = None
        self.state = read_json(self.path) if self.path.exists() else {}
        if self.state:
            require(self.state.get('fixed_sha256') == FIXED_SHA256, '本地缓存方案版本不一致。')
            validate_fixed_assignment(self.state['assignment'], uid)
        self.clock_anchor = time.monotonic()
        self.clock_elapsed = float(self.state.get('clock_elapsed', 0))
        if self.state:
            self.clock_elapsed += max(0., time.time() - self.state['clock_saved_wall'])
        self.audit = Audit()
        self.adapter = TcpFleetAdapter([uid], bind_host=bind_host,
            port=self.vehicle['task_port'] if tcp_port is None else tcp_port,
            auth_token=tcp_token, identity_validator=lambda hello: validate_identity(hello, fleet))
        self.adapter.audit = self.audit
        self.adapter.competition_time_provider = self.clock
        self.stop_event = threading.Event()
        self.monitor = None

    def clock(self):
        with self.state_lock:
            if not self.state:
                return {'running': False}
            result = copy.deepcopy(self.state['assignment']['competition_time'])
            result['elapsed_seconds'] = self.clock_elapsed + max(0., time.monotonic() - self.clock_anchor)
            return result

    def _save(self):
        # Called with state_lock; local wall time is only used to restore this relay's clock.
        self.state['clock_elapsed'] = self.clock()['elapsed_seconds']
        self.state['clock_saved_wall'] = time.time()
        save_json(self.path, self.state)

    def start(self):
        # On Windows SO_REUSEADDR can permit an unintended second bind. Check the
        # original listener explicitly before the frozen adapter opens its socket.
        if self.adapter.port:
            hosts = ({'127.0.0.1', self.vehicle['ground_host']} if self.adapter.bind_host == '0.0.0.0'
                     else {self.adapter.bind_host})
            for host in hosts:
                try:
                    connection = socket.create_connection((host, self.adapter.port), timeout=.3)
                except OSError:
                    continue
                connection.close()
                raise Failure('任务端口 {} 已有监听程序；先关闭原后端/重复备用服务。'.format(self.adapter.port))
        self.adapter.start()  # Binding failure must stop startup; never select a different port.
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.monitor.start()

    def stop(self):
        self.stop_event.set()
        self.adapter.stop()
        if self.monitor:
            self.monitor.join(timeout=2)

    def _monitor(self):
        previous = None
        while not self.stop_event.wait(.3):
            try:
                status = self.status()
                signature = (status['connected'], status['assignment_acked'],
                             (status['telemetry'] or {}).get('task_phase'))
                if signature != previous:
                    print('UAV{} 连接={} 回执={} 阶段={}'.format(self.uid, *signature), flush=True)
                    previous = signature
            except Exception as error:
                print('监视错误：{}'.format(error), flush=True)

    def status(self):
        telemetry = self.adapter.telemetry_snapshot(self.uid)
        raw = asdict(telemetry) if telemetry else None
        connected = bool(telemetry and telemetry.connected and self.uid in self.adapter.connected_uav_ids)
        age = telemetry.gps_telemetry_age_seconds if telemetry else None
        fresh = connected and age is not None and age <= 3.
        with self.state_lock:
            assignment = self.state.get('assignment') or {}
            match = bool(telemetry and assignment and telemetry.task_assignment_acked
                and telemetry.task_assignment_mission_id == assignment['mission_id']
                and telemetry.task_assignment_checksum == assignment['assignment_checksum'])
            boot = (raw or {}).get('capabilities', {}).get('boot_id')
            changed = bool(self.state and boot != self.state['boot_id'])
            if fresh and match and not changed:
                phase = telemetry.task_phase
                if phase in STARTED_PHASES and not self.state.get('observed_started'):
                    self.state['observed_started'] = True
                    self._save()
                if phase in FAILED_PHASES and self.state.get('failure_phase') != phase:
                    self.state['failure_phase'] = phase
                    self._save()
            return dict(uav_id=self.uid, fixed_sha256=FIXED_SHA256, connected=connected,
                fresh=fresh, telemetry=raw, assignment_acked=match,
                mission_id=assignment.get('mission_id'), assignment_checksum=assignment.get('assignment_checksum'),
                boot_changed=changed, observed_started=self.state.get('observed_started', False),
                failure_phase=self.state.get('failure_phase'), last_error=self.audit.last_error,
                takeoff=copy.deepcopy(self.state.get('takeoff')), competition_time=self.clock(),
                control_settings={key: self.config[key] for key in
                                  ('duration_seconds', 'return_battery_threshold', 'preflight_battery_min')},
                identity_errors=dict(self.adapter.identity_errors))

    def _ready(self, expected=None, motion=False):
        state = self.status()
        require(state['fresh'] and state['connected'], '未收到本机新鲜、已连接的飞控遥测。')
        t = state['telemetry']
        caps = t['capabilities']
        require(t['identity'].get('identity_verified') is True and bool(caps.get('boot_id')), '机载身份/boot_id 尚未就绪。')
        require(not t['armed'], '飞机已解锁，不能重新分派或补发起飞。')
        require(t['task_phase'] in ('', 'idle', 'assigned', 'takeoff_failed', 'landed', 'completed'),
                '机载当前阶段不允许重新分派/起飞：' + t['task_phase'])
        if expected:
            require(state['mission_id'] == expected['mission_id']
                    and state['assignment_checksum'] == expected['assignment_checksum'], '任务已变化。')
            require(state['assignment_acked'] and not state['boot_changed'], '完整任务未确认或机载重新开机。')
        if motion:
            require(not state['observed_started'] and not state['failure_phase'], '任务已启动或已失败；禁止重新起飞。')
            require(t['task_phase'] == 'assigned', '机载未处于 assigned 阶段。')
            require(t['odom_valid'] and not t['failsafe'], '定位无效或 failsafe。')
            require(math.isfinite(t['battery_percentage'])
                    and t['battery_percentage'] >= self.config['preflight_battery_min'], '电量不足起飞阈值。')
            require(caps.get('motion_enabled') is True and caps.get('auto_takeoff_supported') is True,
                    '机载未开启自动起飞。')
            require(caps.get('auto_takeoff_ready') is True,
                    '机载起飞条件未通过：' + str(caps.get('auto_takeoff_reason')))
        return state

    def assign(self, body):
        received = time.monotonic()
        assignment = body['assignment']
        validate_fixed_assignment(assignment, self.uid)
        elapsed = body['elapsed_seconds']
        require(type(elapsed) in (int, float) and math.isfinite(elapsed)
                and assignment['competition_time']['elapsed_seconds'] <= elapsed <= 366*86400, '比赛时间无效。')
        with self.control_lock:
            current = self._ready()
            probe = self.adapter.clock_probe(self.uid)
            boot = current['telemetry']['capabilities']['boot_id']
            require(probe['boot_id'] == boot, '机载 boot_id 已变化，请等待新遥测。')
            with self.state_lock:
                old = self.state.get('assignment')
                duplicate = old is not None and old['assignment_checksum'] == assignment['assignment_checksum']
                if duplicate:
                    require(not self.state.get('takeoff') and not self.state.get('observed_started'), '已发送过起飞，不能重发任务。')
                    require(self.state['boot_id'] == boot, '旧任务跨开机；请创建新任务。')
                    self.clock_elapsed = max(self.clock()['elapsed_seconds'], elapsed+time.monotonic()-received)
                else:
                    require(not old or old['mission_id'] != assignment['mission_id'], '同一任务编号不能改写内容。')
                    self.state = dict(fixed_sha256=FIXED_SHA256, assignment=copy.deepcopy(assignment),
                                      boot_id=boot, observed_started=False)
                    self.clock_elapsed = float(elapsed)+time.monotonic()-received
                self.clock_anchor = time.monotonic()
                self._save()  # Journal before crossing the socket.
                self.prepared = None
            self.adapter.command_assign_task(self.uid, assignment)
            return {'sent': True, 'assignment_checksum': assignment['assignment_checksum']}

    def preflight(self, body):
        with self.control_lock:
            state = self._ready(body, motion=True)
            report = check_program_b(self.vehicle, self.config)
            probe = self.adapter.clock_probe(self.uid)
            require(probe['boot_id'] == state['telemetry']['capabilities']['boot_id'], '机载刚重新开机。')
            self._ready(body, motion=True)
            self.prepared = dict(token=secrets.token_urlsafe(24), expires=time.monotonic()+15,
                                 mission_id=body['mission_id'], checksum=body['assignment_checksum'],
                                 boot_id=probe['boot_id'], remote_wall=probe['remote_wall'],
                                 sampled=time.monotonic(), program_b=report)
            return dict(ready=True, token=self.prepared['token'], ttl_seconds=15, program_b=report)

    def takeoff(self, body):
        with self.control_lock:
            # Repeated HTTP requests are observations unless an explicit retry was requested.
            current = self.status()
            require(current['mission_id'] == body['mission_id']
                    and current['assignment_checksum'] == body['assignment_checksum'], '任务已变化。')
            prior = current['takeoff']
            if prior:
                require(prior['command_id'] == body['command_id'], '起飞操作编号不一致。')
                if current['observed_started'] or not body.get('retry_missing'):
                    return dict(sent=False, already_attempted=True, observed_started=current['observed_started'])
                require(time.time()-prior['attempted_at'] >= 5, '距离上次发送不足 5 秒，先等待机载反馈。')
            self._ready(body, motion=True)
            prepared = self.prepared
            require(prepared and hmac.compare_digest(str(body.get('token', '')), prepared['token'])
                    and time.monotonic() < prepared['expires'], '起飞预检凭证缺失或过期。')
            require(prepared['mission_id'] == body['mission_id']
                    and prepared['checksum'] == body['assignment_checksum'], '预检任务已变化。')
            self.prepared = None
            now_remote = prepared['remote_wall'] + time.monotonic() - prepared['sampled']
            with self.state_lock:
                require(prepared['boot_id'] == self.state['boot_id'], '机载开机标识已变化。')
                payload = prior['payload'] if prior else dict(
                    mission_id=body['mission_id'], target_altitude_m=self.state['assignment']['target_altitude_m'],
                    deadline_at=now_remote + self.config['duration_seconds'],
                    return_battery_threshold=self.config['return_battery_threshold'])
                require(now_remote < payload['deadline_at'], '本次起飞期限已过，不能补发旧命令。')
                self.state['takeoff'] = dict(command_id=body['command_id'], payload=payload,
                    attempted_at=time.time(), send_count=(prior or {}).get('send_count', 0)+1)
                self._save()
            self.adapter.command_takeoff(self.uid, payload)
            print('UAV{} 已发送 takeoff，等待机载阶段反馈。'.format(self.uid), flush=True)
            return dict(sent=True, payload=payload)


def make_http_server(relay, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            try:
                supplied = self.headers.get('X-Emergency-Token', '')
                require(hmac.compare_digest(supplied, relay.token), '认证失败。')
                if self.command == 'GET' and self.path == '/status':
                    result = relay.status()
                else:
                    require(self.command == 'POST' and self.path in ('/assignment', '/preflight', '/takeoff'), '未知操作。')
                    size = int(self.headers.get('Content-Length', '0'))
                    require(0 < size <= 4*1024*1024, '请求长度无效。')
                    self.connection.settimeout(10)
                    body = json.loads(self.rfile.read(size).decode('utf-8'))
                    require(isinstance(body, dict), 'JSON 根节点须为对象。')
                    result = {'/assignment': relay.assign, '/preflight': relay.preflight,
                              '/takeoff': relay.takeoff}[self.path](body)
                status = 200
            except Exception as error:
                status, result = 409, {'error': str(error)}
            raw = canonical(result)
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass  # The command journal survives a lost HTTP response.

        do_GET = handle_request
        do_POST = handle_request

    return ThreadingHTTPServer((host, port), Handler)
