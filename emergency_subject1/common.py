"""Standalone emergency tools. No installed backend, ROS, or third-party imports."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))
from competition_shared.fleet import validate_fleet
from competition_shared.recognition import validate_recognition_selection
from frozen_protocol.task_protocol import assignment_checksum, validate_assignment
from frozen_protocol.transit_protocol import route_messages

IDS = tuple(range(1, 7))
FIXED_SHA256 = '8ae1dbc31917ef07bd539a6213473f165d5206e27c155c920aa39ffd74c9af91'
STARTED_PHASES = {'preparing_takeoff', 'arming', 'entering_command_control',
                  'taking_off', 'external_waiting', 'executing', 'completed',
                  'returning', 'landing', 'landed', 'external_return_requested',
                  'external_return_descent', 'external_land', 'external_return',
                  'external_reconnaissance', 'external_takeoff'}
FAILED_PHASES = {'manual_override', 'takeoff_failed', 'paused'}


class Failure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_fixed():
    raw = (ROOT / 'fixed_mission.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == FIXED_SHA256,
            '固定任务文件校验失败；不要手工修改航点、降落点或路线。')
    return json.loads(raw.decode('utf-8'))


def load_settings(config_path=ROOT / 'config.json', fleet_path=ROOT / 'fleet.json'):
    config = read_json(config_path)
    fleet = validate_fleet(read_json(fleet_path))
    require({v['uav_id'] for v in fleet['vehicles'] if v['enabled']} == set(IDS),
            '备用方案要求 UAV1～6 全部启用。')
    require(type(config['publisher_uav_id']) is int and config['publisher_uav_id'] in IDS,
            'publisher_uav_id 必须为 1～6。')
    ids = config['recognition_category_ids']
    require(isinstance(ids, list), 'recognition_category_ids 必须是数组。')
    validate_recognition_selection(dict(category_ids=ids, category_count=len(ids)))
    for key in ('duration_seconds', 'return_battery_threshold', 'preflight_battery_min'):
        value = config[key]
        require(type(value) in (int, float) and math.isfinite(value), key + ' 必须为有效数值。')
    require(0 < config['duration_seconds'] <= 86400, '任务期限无效。')
    require(0 < config['return_battery_threshold'] < config['preflight_battery_min'] <= 1,
            '电量阈值应满足 0 < 返航阈值 < 起飞阈值 <= 1。')
    require(type(config['relay_port']) is int and 1024 <= config['relay_port'] <= 65535,
            'relay_port 无效。')
    return config, fleet


def peer_token():
    token = os.environ.get('COMPETITION_PEER_TOKEN') or os.environ.get('PEER_TOKEN', '')
    require(bool(token), '请先加载原 tools/local_tokens.ps1（PEER_TOKEN 或 COMPETITION_PEER_TOKEN）。')
    return token


def urls(config, fleet):
    return {v['uav_id']: 'http://{}:{}'.format(v['peer_host'], config['relay_port'])
            for v in fleet['vehicles'] if v['enabled']}


def request(url, token, path, payload=None, timeout=12):
    # Radio/LAN control traffic must not pass through the computer's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url.rstrip('/') + path,
                                 data=None if payload is None else canonical(payload),
                                 headers={'Content-Type': 'application/json',
                                          'X-Emergency-Token': token},
                                 method='GET' if payload is None else 'POST')
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode('utf-8', errors='replace')
        raise Failure('HTTP {} {}: {}'.format(error.code, path, body)) from error
    except (OSError, ValueError) as error:
        raise Failure('{} {}: {}'.format(url, path, error)) from error


def parallel(ids, function):
    values, errors = {}, {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(function, uid): uid for uid in ids}
        for future in as_completed(futures):
            uid = futures[future]
            try:
                values[uid] = future.result()
            except Exception as error:
                errors[uid] = str(error)
    return values, errors


def error_text(errors):
    return '\n'.join('UAV{}: {}'.format(uid, value) for uid, value in sorted(errors.items()))


def clock_snapshot(base, elapsed):
    result = copy.deepcopy(base)
    result['elapsed_seconds'] = float(elapsed)
    return result


def validate_fixed_assignment(message, uid):
    checked = validate_assignment(message, uid)
    plain = copy.deepcopy(message)
    for key in ('mission_id', 'assignment_checksum', 'competition_time'):
        plain.pop(key, None)
    selection = plain['task'].pop('recognition_selection')
    validate_recognition_selection(selection)
    require(plain == load_fixed()['assignments'][str(uid)],
            'UAV{} 任务不是本工具固化的比赛方案。'.format(uid))
    require(checked['competition_time']['running'] and checked['competition_time']['synchronized'],
            '比赛计时必须正在运行。')
    return checked


def build_bundle(config, mission_id, session_id, elapsed_seconds):
    require(type(elapsed_seconds) in (int, float) and math.isfinite(elapsed_seconds)
            and 0 <= elapsed_seconds <= 366 * 86400, '比赛已用时间无效。')
    templates = load_fixed()['assignments']
    selection = validate_recognition_selection(dict(
        category_ids=config['recognition_category_ids'],
        category_count=len(config['recognition_category_ids'])))
    stamp = dict(schema_version=1, mission_id=mission_id, running=True, synchronized=True,
                 session_id=session_id, revision=0, publisher_terminal_id=config['publisher_uav_id'],
                 updated_by=config['publisher_uav_id'], elapsed_seconds=float(elapsed_seconds))
    for uid in IDS:
        message = templates[str(uid)]
        message.update(mission_id=mission_id, competition_time=copy.deepcopy(stamp))
        message['task']['recognition_selection'] = copy.deepcopy(selection)
        message['assignment_checksum'] = assignment_checksum(message)
        validate_fixed_assignment(message, uid)
    return dict(schema_version=1, mission_id=mission_id, fixed_sha256=FIXED_SHA256,
                created_at=time.time(), competition_time=stamp,
                duration_seconds=config['duration_seconds'],
                return_battery_threshold=config['return_battery_threshold'],
                assignments=templates)


def validate_bundle(bundle):
    require(bundle.get('fixed_sha256') == FIXED_SHA256, '任务包固定方案版本不一致。')
    require(set(bundle['assignments']) == {str(uid) for uid in IDS}, '任务包必须包含六机。')
    for uid in IDS:
        message = bundle['assignments'][str(uid)]
        validate_fixed_assignment(message, uid)
        require(message['mission_id'] == bundle['mission_id'], '任务包任务编号不一致。')
        require(message['competition_time'] == bundle['competition_time'], '任务包计时不一致。')
    return bundle


def fleet_status(endpoints, token):
    states, errors = parallel(IDS, lambda uid: request(endpoints[uid], token, '/status'))
    for uid, state in states.items():
        if state.get('uav_id') != uid or state.get('fixed_sha256') != FIXED_SHA256:
            errors[uid] = '备用节点编号或固定方案版本不一致。'
    require(not errors, error_text(errors))
    return states


def show_status(states):
    for uid in IDS:
        s = states[uid]
        t = s.get('telemetry') or {}
        print('UAV{} 连接={} 新鲜={} 任务回执={} 阶段={} 起飞已观察={} {}'.format(
            uid, s.get('connected'), s.get('fresh'), s.get('assignment_acked'),
            t.get('task_phase', '-'), s.get('observed_started'), s.get('last_error', '')),
              flush=True)


def require_bundle_on_fleet(bundle, states):
    errors = {}
    for uid in IDS:
        s, assignment = states[uid], bundle['assignments'][str(uid)]
        if (s.get('mission_id') != bundle['mission_id']
                or s.get('assignment_checksum') != assignment['assignment_checksum']
                or not s.get('assignment_acked') or not s.get('fresh') or not s.get('connected')):
            errors[uid] = '当前连接/新鲜遥测/完整任务回执不匹配。'
        elif s.get('boot_changed'):
            errors[uid] = '机载已重新开机；不能继续旧任务起飞。'
        elif any(s.get('control_settings', {}).get(key) != bundle[key]
                 for key in ('duration_seconds', 'return_battery_threshold')):
            errors[uid] = '备用节点的任务期限/返航电量阈值与发布任务不一致。'
    require(not errors, error_text(errors))


class RunLock:
    """OS releases the controller lock even if the CLI is killed."""
    def __init__(self, folder):
        self.path = Path(folder) / 'controller.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise Failure('另一个分派/起飞脚本正在运行。') from error
        return self

    def __exit__(self, *_args):
        self.stream.close()


def main_guard(function):
    try:
        function()
    except (Failure, ValueError, KeyError, OSError) as error:
        print('失败：{}'.format(error), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print('已停止。未自动发送起飞、返航或重启指令。', file=sys.stderr, flush=True)
        return 130
    return 0
