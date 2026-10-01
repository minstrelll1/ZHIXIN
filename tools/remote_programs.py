"""由地面端通过 SSH 传送执行；只管理竞赛目录内的进程记录和日志。"""
import base64
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

NAMES = {"onboard": "竞赛程序机载端", "detection": "目标检测程序", "flight": "自主飞行指令程序"}


def command_for(key, uid, model, commands=None):
    if commands is not None and key in commands:
        command = commands[key]
        if not isinstance(command, str) or not command.strip() or '\x00' in command:
            raise ValueError('%s启动指令必须为非空字符串' % NAMES[key])
        # 仅替换约定变量，保留 Bash 的 ${HOME}、函数或其他花括号。
        return command.replace('{uav_id}', str(uid)).replace('{model}', model)
    if key == "onboard":
        return "exec bash ./tools/start_onboard_stack.sh --model %s --expect-uav-id %d --direct --enable-motion" % (model, uid)
    if key == "detection":
        return "source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash && exec roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=%d" % uid
    # 兼容未提供启动配置的旧地面端；竞赛程序不按场景强制切换程序 B 模式。
    return "source ~/recon_ws/devel/setup.bash && exec roslaunch px4_north_camera p600_gx40_position_pid_reconnaissance.launch uav_id:=%d flight_mode:=outdoor" % uid


def stamp(pid):
    try:
        # comm 可以包含空格，字段 22 是 Linux 进程创建时钟。
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return fields[19] if fields[0] != 'Z' else ''
    except (OSError, ValueError):
        return ''


def alive(meta):
    return bool(meta.get('pid') and stamp(meta['pid']) == meta.get('stamp') and meta.get('stamp'))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    temp = path.with_suffix('.tmp.%d' % os.getpid())
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def existing(key, uid):
    expected = {'onboard': 'onboard_preflight.py', 'detection': 'uav_yolo26_botsort_geolocation.launch',
                'flight': 'p600_gx40_position_pid_reconnaissance.launch'}[key]
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if '--worker' in args:
                continue
            text = ' '.join(args)
            if expected not in text:
                # onboard_preflight 最终 exec roslaunch；检查本工程的 launch。
                if key != 'onboard' or 'su17_competition_executor' not in text or 'roslaunch' not in text:
                    continue
            match = re.search(r'(?:uav_id:=|--expect-uav-id\s+)([1-6])(?:\s|$)', text)
            if match and int(match[1]) == uid:
                pid = int(proc.name)
                return {'pid': pid, 'stamp': stamp(pid), 'borrowed': True}
        except (OSError, ValueError):
            continue
    return {}


def worker(directory, command):
    directory = Path(directory)
    meta_path = directory / 'process.json'
    with (directory / 'console.log').open('ab', buffering=0) as log:
        log.write(('\n[%s] 启动指令：%s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), command)).encode())
        try:
            env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', ROSCONSOLE_FORMAT='[${severity}] [${time}]: ${message}')
            child = subprocess.Popen(['bash', '-lc', 'set -eo pipefail; ' + command],
                                     cwd=str(Path.home() / 'competition_development'),
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
            write_json(meta_path, {'pid': child.pid, 'stamp': stamp(child.pid), 'started_at': time.time(), 'borrowed': False})
            code = child.wait()
            meta = read_json(meta_path)
            meta.update(exit_code=code, stopped_at=time.time())
            write_json(meta_path, meta)
            log.write(('\n程序已停止，退出码：%s\n' % code).encode())
        except Exception as error:
            log.write(('\n程序启动失败：%s\n' % error).encode())
            write_json(meta_path, {'exit_code': -1, 'error': str(error)})


def status(directory, offset):
    meta = read_json(directory / 'process.json')
    live = alive(meta)
    log_path = directory / 'console.log'
    size = log_path.stat().st_size if log_path.exists() else 0
    offset = max(0, min(int(offset), size))
    data, tail = b'', b''
    if size:
        with log_path.open('rb') as log:
            log.seek(offset)
            data = log.read(65536)
            log.seek(max(0, size - 32768))
            tail = log.read()
    # roslaunch 自身仍活着时，关键子进程也可能已经崩溃。
    text = tail.decode('utf-8', errors='replace')
    if '启动指令：' in text:
        text = text[text.rfind('启动指令：'):]
    node_states = {}
    for match in re.finditer(r'(?:\[|process\[)([^\]]+)\][^\n]*?(process has died|started with pid)', text):
        node_states[match[1]] = match[2]
    unhealthy = any(value == 'process has died' for value in node_states.values()) or 'RLException:' in text
    if 'Traceback (most recent call last)' in text and not node_states:
        unhealthy = True
    unhealthy = unhealthy and not meta.get('borrowed') and not meta.get('stopped_by_operator')
    state = 'error' if unhealthy else ('running' if live else ('stopped' if meta else 'idle'))
    detail = '进程运行中' if live else '程序尚未启动或已停止'
    if unhealthy:
        detail = '检测到子进程异常，请查看打印内容'
    if meta.get('borrowed') and live:
        detail = '复用原有进程；原终端历史输出无法追溯'
    return dict(state=state, detail=detail, pid=meta.get('pid'), exit_code=meta.get('exit_code'),
                log=base64.b64encode(data).decode(), offset=offset + len(data), size=size)


def expected_nodes(uid):
    prefix = '/uav%d/' % uid
    return {
        'onboard': ['/competition_image_stamp_adapter', '/su17_competition_executor', '/su17_onboard_image_sender'],
        'detection': ['/uav_yolo26_botsort_geolocation'],
        'flight': [prefix + name for name in ('target_geolocator', 'gimbal_tracker_gx40_vertical',
                   'target_maneuver_gx40_position_pid', 'target_scheduler_gx40_position_pid', 'trajectory_follower')],
    }


def ros_health(uid):
    import xmlrpc.client
    from concurrent.futures import ThreadPoolExecutor
    class Transport(xmlrpc.client.Transport):
        def make_connection(self, host):
            connection = super().make_connection(host)
            connection.timeout = .6
            return connection
    master_uri = os.environ.get('ROS_MASTER_URI', 'http://127.0.0.1:11311')
    caller = '/competition_program_monitor'
    expected = expected_nodes(uid)
    def ping(name):
        try:
            with xmlrpc.client.ServerProxy(master_uri, transport=Transport()) as master:
                code, _, address = master.lookupNode(caller, name)
                if code != 1:
                    return name, 0
            with xmlrpc.client.ServerProxy(address, transport=Transport()) as node:
                code, _, pid = node.getPid(caller)
                return name, int(pid) if code == 1 else 0
        except Exception:
            return name, 0
    names = [name for group in expected.values() for name in group]
    with ThreadPoolExecutor(max_workers=8) as pool:
        available = dict(pool.map(ping, names))
    result = {key: {'ready': all(available[name] for name in group),
                    'present': [name for name in group if available[name]],
                    'missing': [name for name in group if not available[name]],
                    'pids': {name: available[name] for name in group if available[name]}} for key, group in expected.items()}
    # 读取实际生效参数，而不是猜测启动命令：人工启动或复用已有程序时也正确。
    result['flight']['flight_mode'] = 'unknown'
    result['flight']['reconnaissance_radius_m'] = None
    if result['flight']['ready']:
        try:
            with xmlrpc.client.ServerProxy(master_uri, transport=Transport()) as master:
                code_indoor, _, indoor = master.getParam(caller, '/uav%d/trajectory_follower/indoor_mode' % uid)
                code_small, _, small = master.getParam(caller, '/uav%d/trajectory_follower/outdoor_small_range_mode' % uid)
                code_radius, _, radius = master.getParam(caller, '/uav%d/target_geolocator/reconnaissance_radius_m' % uid)
            def as_ros_bool(value):
                if isinstance(value, bool):
                    return value
                if isinstance(value, int) and value in (0, 1):
                    return bool(value)
                if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
                    return value.strip().lower() == 'true'
                raise ValueError('ROS 布尔参数无效')
            if code_indoor == 1 and code_small == 1:
                indoor_mode, small_mode = as_ros_bool(indoor), as_ros_bool(small)
                if not (indoor_mode and small_mode):
                    result['flight']['flight_mode'] = ('indoor' if indoor_mode else
                        'outdoor_small_range' if small_mode else 'outdoor')
            if code_radius == 1:
                value = float(radius)
                if 0.0 < value < 10000.0:
                    result['flight']['reconnaissance_radius_m'] = value
        except Exception:
            pass
    return result


def apply_health(info, health, meta):
    info['nodes'] = health
    if 'flight_mode' in health:
        info['flight_mode'] = health['flight_mode']
    if 'reconnaissance_radius_m' in health:
        info['reconnaissance_radius_m'] = health['reconnaissance_radius_m']
    if health['ready']:
        info.update(state='running', detail='ROS 节点已就绪（%d/%d）' % (len(health['present']), len(health['present'])))
    elif health['present'] or info['state'] == 'running':
        starting = time.time() - meta.get('started_at', 0) < 30
        info.update(state='starting' if starting else 'error', detail='等待 ROS 节点：' + '、'.join(health['missing']))
    elif info['state'] == 'error':
        pass
    else:
        info['detail'] = '程序未运行；未检测到对应 ROS 节点'
    return info


def process_table():
    table = {}
    for directory in Path('/proc').glob('[0-9]*'):
        try:
            pid = int(directory.name)
            stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            args = (directory / 'cmdline').read_bytes().decode(errors='replace').split('\0')
            if stat[0] == 'Z':
                continue
            table[pid] = dict(pid=pid, stamp=stat[19], parent=int(stat[1]), args=args)
        except (OSError, ValueError, IndexError):
            continue
    return table


def protected_process(item):
    # ROS master/rosout 为共享基础设施，不属于某一个业务程序。
    return any(Path(arg).name in ('rosmaster', 'roscore', 'rosout') for arg in item.get('args', []) if arg)


def collect_tree(table, roots):
    targets = {pid: table[pid] for pid, identity in roots.items()
               if pid in table and table[pid]['stamp'] == identity['stamp'] and not protected_process(table[pid])}
    while True:
        added = {pid: item for pid, item in table.items() if item['parent'] in targets
                 and pid not in targets and not protected_process(item)}
        if not added:
            return targets
        targets.update(added)


def stop_program(directory, key, uid):
    import signal
    roots = {}
    meta = read_json(directory / 'process.json')
    for item in (meta, read_json(directory / 'worker.json'), existing(key, uid)):
        if alive(item):
            roots[item['pid']] = item
    table = process_table()
    # 手工启动或启动指令被修改时，可由已验证的本机 ROS 节点找到进程。
    health = ros_health(uid)[key]
    for name, pid in health.get('pids', {}).items():
        item = table.get(pid)
        if item and any(name.rsplit('/', 1)[-1] in arg for arg in item['args']):
            roots[pid] = item
            # 程序 B 的日志包装器也要退出；不纳入未知 roslaunch 或交互 shell。
            parent = table.get(item['parent'])
            if parent and any(Path(arg).name == 'ros_log_capture.py' for arg in parent['args']):
                roots[parent['pid']] = parent
    targets = collect_tree(table, roots)
    if any(protected_process(item) and item['parent'] in targets for item in table.values()):
        raise RuntimeError('该启动进程同时管理共享 ROS 主节点，无法单独结束；未执行停止，请在原启动终端处理')
    if os.getpid() in targets:
        raise RuntimeError('拒绝将管理连接自身作为停止目标')
    with (directory / 'console.log').open('ab') as log:
        log.write(('\n请求停止%s；已识别进程：%s\n' % (NAMES[key], ','.join(map(str, targets)) or '无')).encode())
    # 先让 roslaunch 接收 SIGINT 完成节点退出；残留节点再 TERM/KILL。
    for sig, seconds in ((signal.SIGINT, 4), (signal.SIGTERM, 2), (signal.SIGKILL, 1)):
        table = process_table()
        targets.update(collect_tree(table, targets))
        pending = {pid: item for pid, item in targets.items() if alive(item)}
        for pid, item in pending.items():
            if not alive(item):
                continue
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            targets.update(collect_tree(process_table(), targets))
            if not any(alive(item) for item in targets.values()):
                break
            time.sleep(.1)
        if not any(alive(item) for item in targets.values()):
            break
    remaining = [pid for pid, item in targets.items() if alive(item)]
    nodes = ros_health(uid)[key]
    verified = not remaining and not nodes['present']
    detail = '程序及已识别子进程已退出' if verified else '仍有进程或 ROS 节点未退出：%s %s' % (remaining, nodes['present'])
    if verified:
        write_json(directory / 'process.json', {'stopped_by_operator': True, 'stopped_at': time.time()})
        write_json(directory / 'worker.json', {})
    with (directory / 'console.log').open('ab') as log:
        log.write((detail + '\n').encode())
    return dict(state='stopped' if verified else 'error', detail=detail, stop_verified=verified,
                remaining_pids=remaining, nodes=nodes)


def rpc(payload):
    import fcntl
    uid, model = int(payload['uav_id']), payload['model']
    if uid not in range(1, 7) or model not in ('p600', 'su17'):
        raise ValueError('无人机编号或机型错误')
    root = Path.home() / 'competition_development'
    if not (root / 'tools/start_onboard_stack.sh').is_file():
        raise RuntimeError('请先部署竞赛机载代码到 ~/competition_development')
    runtime = root / 'ground_runtime' / ('programs_uav%d' % uid)
    runtime.mkdir(parents=True, exist_ok=True)
    result = {}
    action = payload.get('action', 'status')
    selected = payload.get('program')
    if action not in ('start', 'status', 'stop') or (selected is not None and selected not in NAMES):
        raise ValueError('程序操作无效')
    if action == 'stop' and selected not in NAMES:
        raise ValueError('停止时必须指定一个程序')
    health = ros_health(uid)
    for key in NAMES:
        directory = runtime / key
        directory.mkdir(exist_ok=True)
        try:
            with (directory / 'launch.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if action == 'stop' and key == selected:
                    result[key] = stop_program(directory, key, uid)
                    continue
                meta = read_json(directory / 'process.json')
                pending = read_json(directory / 'worker.json')
                found = existing(key, uid) if not alive(meta) else {}
                if action == 'start' and (selected is None or selected == key) and health[key]['ready'] and not meta:
                    write_json(directory / 'process.json', {'borrowed': True, 'node_only': True})
                    with (directory / 'console.log').open('ab') as log:
                        log.write(('复用已经就绪的%s ROS 节点；原终端历史输出无法追溯。\n' % NAMES[key]).encode())
                if found and payload.get('action') != 'start':
                    write_json(directory / 'process.json', found)
                if action == 'start' and (selected is None or selected == key) and not alive(meta) and not alive(pending) and not health[key]['ready']:
                    if health[key]['present'] and not found:
                        message = '已有部分 ROS 节点运行，暂不重复启动：' + '、'.join(health[key]['present'])
                        with (directory / 'console.log').open('ab') as log:
                            log.write((message + '\n').encode())
                        info = status(directory, payload.get('offsets', {}).get(key, 0))
                        info.update(state='error', detail=message, nodes=health[key])
                        result[key] = info
                        continue
                    if found:
                        write_json(directory / 'process.json', found)
                        with (directory / 'console.log').open('ab') as log:
                            log.write(('复用已运行的%s，PID=%s；此前终端输出无法追溯。\n' % (NAMES[key], found['pid'])).encode())
                    else:
                        helper = directory / 'runner.py'
                        helper.write_text(PROGRAM_SOURCE, encoding='utf-8')
                        with (directory / 'console.log').open('ab') as log:
                            process = subprocess.Popen([sys.executable, str(helper), '--worker', str(directory), command_for(key, uid, model, payload.get('commands'))],
                                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
                        write_json(directory / 'worker.json', {'pid': process.pid, 'stamp': stamp(process.pid)})
                        # 启动锁覆盖 worker 写入 child PID 的时间，防止并发确认重复启动。
                        until = time.monotonic() + 2
                        while time.monotonic() < until and process.poll() is None:
                            new = read_json(directory / 'process.json')
                            if new != meta and new:
                                break
                            time.sleep(.03)
                info = status(directory, payload.get('offsets', {}).get(key, 0))
                result[key] = apply_health(info, health[key], read_json(directory / 'process.json'))
        except Exception as error:
            result[key] = dict(state='error', detail='程序操作失败：%s' % error)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--worker':
    worker(sys.argv[2], sys.argv[3])
