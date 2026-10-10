"""由地面端通过 SSH 传送执行；只管理竞赛目录内的进程记录和日志。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

NAMES = {"onboard": "竞赛程序机载端", "detection": "目标检测程序", "flight": "自主飞行指令程序"}


def boot_id():
    """仅操作系统重新开机才改变；无线断连、ROS 重启、时钟校准均不改变。"""
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        return value if re.fullmatch(r'[0-9a-fA-F-]{36}', value) else ''
    except (OSError, ValueError):
        return ''


def detection_command(uid):
    return ("source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash\n"
            "exec roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch "
            "uav_id:=%d runtime_mode:=debug debug_save_dir:=/tmp/yolo_debug" % uid)


def command_for(key, uid, model, commands=None):
    if commands is not None and key in commands:
        command = commands[key]
        if not isinstance(command, str) or not command.strip() or '\x00' in command:
            raise ValueError('%s启动指令必须为非空字符串' % NAMES[key])
        if key == 'detection' and (command == (
                'source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash && exec roslaunch '
                'spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:={uav_id}')
                or command == detection_command(2)
                or re.fullmatch(r'roslaunch spirecv_ros uav_yolo26_botsort_geolocation\.launch '
                                r'uav_id:=(?:[1-6]|\{uav_id\}) runtime_mode:=debug '
                                r'debug_save_dir:=/tmp/yolo_debug', command)):
            # 已部署电脑会保留本机配置；只兼容升级已知旧默认指令，不改动其他自定义指令。
            return detection_command(uid)
        # 仅替换约定变量，保留 Bash 的 ${HOME}、函数或其他花括号。
        return command.replace('{uav_id}', str(uid)).replace('{model}', model)
    if key == "onboard":
        return "exec bash ./tools/start_onboard_stack.sh --model %s --expect-uav-id %d --direct --enable-motion" % (model, uid)
    if key == "detection":
        return detection_command(uid)
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
    if meta.get('boot_id') and meta['boot_id'] != boot_id():
        return False
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


def current_record(path, current_boot):
    record = read_json(path)
    return record if current_boot and record.get('boot_id') == current_boot else {}


def existing(key, uid):
    for item in matching_launchers(process_table(), key, uid).values():
        return dict(item, borrowed=True, boot_id=boot_id())
    return {}


def matching_launchers(table, key, uid):
    """匹配真实 argv 项，不把包含启动字符串的 shell/管理连接当作程序。"""
    expected = {'onboard': ('onboard_preflight.py', 'start_onboard_stack.sh', 'onboard_competition_stack.launch'),
                'detection': ('uav_yolo26_botsort_geolocation.launch',),
                'flight': ('p600_gx40_position_pid_reconnaissance.launch',)}[key]
    result = {}
    for pid, item in table.items():
        args = item.get('args', [])
        if '--worker' in args or not any(Path(arg).name in expected for arg in args if arg):
            continue
        match = re.search(r'(?:\buav_id:=|--expect-uav-id\s+)([1-6])(?:\s|$)', ' '.join(args))
        if match and int(match[1]) == uid:
            result[pid] = item
    return result


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
            write_json(meta_path, {'pid': child.pid, 'stamp': stamp(child.pid), 'started_at': time.time(),
                                  'borrowed': False, 'boot_id': boot_id()})
            code = child.wait()
            meta = read_json(meta_path)
            meta.update(exit_code=code, stopped_at=time.time())
            write_json(meta_path, meta)
            log.write(('\n程序已停止，退出码：%s\n' % code).encode())
        except Exception as error:
            log.write(('\n程序启动失败：%s\n' % error).encode())
            write_json(meta_path, {'exit_code': -1, 'error': str(error), 'boot_id': boot_id()})


def latest_log_window(directory, log, size):
    """只索引最近一次启动的字节范围；不删改机载原始日志或重启程序。"""
    marker = re.compile(rb'(?m)^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] ' + '启动指令：'.encode('utf-8'))
    stat = os.fstat(log.fileno())
    identity = [stat.st_dev, stat.st_ino]
    index_path = directory / 'log_index.json'
    index = read_json(index_path)
    start, header, lower = 0, b'', 0
    if index.get('identity') == identity and 0 <= index.get('size', -1) <= size:
        old_start = index.get('start', 0)
        old_header = index.get('header', '').encode('utf-8')
        log.seek(old_start)
        if old_header and log.read(len(old_header)) == old_header:
            start, header = old_start, old_header
            lower = max(start, index['size'] - 128)
    end, overlap = size, b''
    while end > lower:
        begin = max(lower, end - 1024 * 1024)
        # 多读一个前置字节，防止把块中间误认为行首；保留跨块的启动标记。
        read_begin = max(0, begin - 1)
        log.seek(read_begin)
        block = log.read(end - read_begin) + overlap
        matches = list(marker.finditer(block))
        if matches:
            match = matches[-1]
            start, header = read_begin + match.start(), match.group()
            break
        overlap = block[begin - read_begin:begin - read_begin + 128]
        end = begin
    record = dict(identity=identity, size=size, start=start, header=header.decode('utf-8'))
    if record != index:
        try:
            write_json(index_path, record)
        except OSError:
            pass  # 索引只是加速缓存；不能因索引写入失败影响状态查询。
    session = hashlib.sha256(json.dumps([identity, start, header.decode('utf-8')]).encode()).hexdigest()
    return start, session


def status(directory, offset, latest_log=False):
    meta = current_record(directory / 'process.json', boot_id())
    live = alive(meta)
    log_path = directory / 'console.log'
    size = log_path.stat().st_size if log_path.exists() else 0
    offset = max(0, min(int(offset), size))
    data, tail = b'', b''
    log_meta = {}
    if size:
        with log_path.open('rb') as log:
            if latest_log:
                start, session = latest_log_window(directory, log, size)
                offset = max(start, offset)
                log_meta = dict(log_start=start, log_session=session, log_read_start=offset)
            log.seek(offset)
            data = log.read(min(65536, size - offset))
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
    if latest_log and meta.get('borrowed'):
        # 人工启动程序的 stdout 没有接入此日志，不能把旧程序输出当作本次打印。
        identity = [meta.get(k) for k in ('boot_id', 'pid', 'stamp')]
        log_meta = dict(log_session='borrowed-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                        log_start=size, log_read_start=size)
        data, offset = b'', size
    return dict(state=state, detail=detail, pid=meta.get('pid'), exit_code=meta.get('exit_code'),
                log=base64.b64encode(data).decode(), offset=offset + len(data), size=size, **log_meta)


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
        address = ''
        try:
            with xmlrpc.client.ServerProxy(master_uri, transport=Transport()) as master:
                code, _, address = master.lookupNode(caller, name)
                if code != 1:
                    return name, 0, '', '', False
            with xmlrpc.client.ServerProxy(address, transport=Transport()) as node:
                code, _, pid = node.getPid(caller)
                return name, int(pid) if code == 1 else 0, '' if code == 1 else '节点未确认响应', address, False
        except Exception as error:
            return name, 0, str(error), address, bool(address and isinstance(error, ConnectionRefusedError))
    names = [name for group in expected.values() for name in group]
    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(ping, names))
    available = {name: pid for name, pid, _, _, _ in replies}
    errors = {name: error for name, _, error, _, _ in replies if error}
    addresses = {name: address for name, _, _, address, _ in replies if address}
    refused = {name for name, _, _, _, closed in replies if closed}
    result = {key: {'ready': all(available[name] for name in group),
                    'present': [name for name in group if available[name]],
                    'missing': [name for name in group if not available[name]],
                    'pids': {name: available[name] for name in group if available[name]},
                    'errors': {name: errors[name] for name in group if name in errors},
                    'addresses': {name: addresses[name] for name in group if name in addresses},
                    'refused': [name for name in group if name in refused]} for key, group in expected.items()}
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


def stop_targets(directory, key, uid, previous=None):
    table = process_table()
    roots = dict(previous or {})
    for item in (read_json(directory / 'process.json'), read_json(directory / 'worker.json'), existing(key, uid)):
        if alive(item):
            roots[item['pid']] = item
    # 不能只停止找到的第一个 roslaunch；重复启动及遗留 worker 全部纳入。
    roots.update(matching_launchers(table, key, uid))
    for pid, item in table.items():
        args = item.get('args', [])
        if '--worker' in args:
            index = args.index('--worker')
            if index + 1 < len(args) and args[index + 1] == str(directory) and any(Path(arg).name == 'runner.py' for arg in args if arg):
                roots[pid] = item
    health = ros_health(uid)[key]
    for name, pid in health.get('pids', {}).items():
        item = table.get(pid)
        node_name = name.rsplit('/', 1)[-1]
        if item and any(arg == '__name:=' + node_name or Path(arg).stem == node_name for arg in item['args']):
            roots[pid] = item
            parent = table.get(item['parent'])
            if parent and any(Path(arg).name == 'ros_log_capture.py' for arg in parent['args']):
                roots[parent['pid']] = parent
    targets = collect_tree(table, roots)
    if any(protected_process(item) and item['parent'] in targets for item in table.values()):
        raise RuntimeError('该启动进程同时管理共享 ROS 主节点，无法单独结束；请在原启动终端处理')
    if os.getpid() in targets:
        raise RuntimeError('拒绝将管理连接自身作为停止目标')
    return targets, health


def stale_stopped_nodes(initial, final, targets):
    """只有旧 URI 拒绝连接且其已核验 PID 确实退出，才视为旧注册残留。"""
    return [name for name in final.get('refused', [])
            if initial.get('addresses', {}).get(name)
            and initial['addresses'][name] == final.get('addresses', {}).get(name)
            and initial.get('pids', {}).get(name) in targets
            and not alive(targets[initial['pids'][name]])]


def stop_program(directory, key, uid):
    import signal
    targets, initial_nodes = stop_targets(directory, key, uid)
    previous_stop = current_record(directory / 'process.json', boot_id())
    if previous_stop.get('stopped_by_operator'):
        previous_nodes = previous_stop.get('stop_nodes', {})
        for field in ('pids', 'addresses'):
            initial_nodes[field] = dict(previous_nodes.get(field, {}), **initial_nodes.get(field, {}))
        for pid, item in previous_stop.get('checked_processes', {}).items():
            targets.setdefault(int(pid), item)
    identified = set(targets)
    signals = []
    with (directory / 'console.log').open('ab') as log:
        log.write(('\n请求停止%s；已识别进程：%s\n' % (NAMES[key], ','.join(map(str, targets)) or '无')).encode())
    # 先让 roslaunch 接收 SIGINT 完成节点退出；残留节点再 TERM/KILL。
    for sig, seconds in ((signal.SIGINT, 4), (signal.SIGTERM, 2), (signal.SIGKILL, 1)):
        # 重新枚举同程序 launcher/节点，捕获停止过程中被 respawn 的进程。
        targets.update(stop_targets(directory, key, uid, targets)[0])
        identified.update(targets)
        pending = {pid: item for pid, item in targets.items() if alive(item)}
        if pending:
            signals.append(int(sig))
        to_signal = pending
        if sig == signal.SIGINT:
            # 优先中断业务 launcher，让其正常注销 ROS 节点；不同时打断监督 worker。
            candidates = {pid: item for pid, item in pending.items()
                          if '--worker' not in item.get('args', [])
                          or not any(child.get('parent') == pid for child in pending.values())}
            to_signal = {pid: item for pid, item in candidates.items() if item.get('parent') not in candidates}
        for pid, item in to_signal.items():
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
        targets.update(stop_targets(directory, key, uid, targets)[0])
        identified.update(targets)
        if not any(alive(item) for item in targets.values()):
            break
    final_targets, nodes = stop_targets(directory, key, uid, targets)
    identified.update(final_targets)
    remaining = [pid for pid, item in final_targets.items() if alive(item)]
    stale_nodes = stale_stopped_nodes(initial_nodes, nodes, targets)
    unresolved = {name: error for name, error in nodes.get('errors', {}).items() if name not in stale_nodes}
    verified = not remaining and not nodes['present'] and not unresolved
    detail = ('程序及子进程已退出，已核验 %d 个进程和对应 ROS 节点' % len(identified)) if verified else (
        '停止未确认，残留进程：%s；ROS 节点：%s；查询失败：%s' % (remaining, nodes['present'], unresolved))
    if verified:
        write_json(directory / 'process.json', {'stopped_by_operator': True, 'stopped_at': time.time(), 'boot_id': boot_id(),
                   'stop_nodes': initial_nodes, 'checked_processes': targets})
        write_json(directory / 'worker.json', {})
    with (directory / 'console.log').open('ab') as log:
        log.write((detail + '\n').encode())
    return dict(state='stopped' if verified else 'error', detail=detail, stop_verified=verified,
                remaining_pids=remaining, checked_pids=sorted(identified), signals=signals, nodes=nodes,
                stale_nodes=stale_nodes)


def rpc(payload):
    import fcntl
    uid, model = int(payload['uav_id']), payload['model']
    if uid not in range(1, 7) or model not in ('p600', 'su17'):
        raise ValueError('无人机编号或机型错误')
    root = Path.home() / 'competition_development'
    if not (root / 'tools/start_onboard_stack.sh').is_file():
        raise RuntimeError('请先部署竞赛机载代码到 ~/competition_development')
    runtime = root / 'ground_runtime' / ('programs_uav%d' % uid)
    action = payload.get('action', 'status')
    selected = payload.get('program')
    if action not in ('start', 'auto_start', 'status', 'stop', 'clock_probe') or (selected is not None and selected not in NAMES):
        raise ValueError('程序操作无效')
    if action == 'stop' and selected not in NAMES:
        raise ValueError('停止时必须指定一个程序')
    current_boot = boot_id()
    if action == 'clock_probe':
        print(json.dumps(dict(boot_id=current_boot, remote_monotonic=time.monotonic(), remote_wall=time.time())))
        return
    runtime.mkdir(parents=True, exist_ok=True)
    result = {}
    if action == 'auto_start' and (not current_boot or payload.get('expected_boot_id') != current_boot):
        raise RuntimeError('机载开机标识尚未确认或已改变，请重新查询；未启动程序')
    skip = payload.get('skip_programs', [])
    if not isinstance(skip, list) or any(key not in NAMES for key in skip):
        raise ValueError('跳过启动的程序名称无效')
    health = ros_health(uid)
    # 一次开机只有一批自动启动。整批意图先写盘，部分启动后断线也不能
    # 在飞行中补启动其余程序；启动失败由操作员查看日志后主动处理。
    session_path = runtime / 'boot_autostart.json'
    auto_allowed = False
    with (runtime / 'boot_autostart.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        known_session = current_record(session_path, current_boot)
        if current_boot and not known_session:
            observed_session = any(
                health[key]['present'] or existing(key, uid)
                or alive(current_record(runtime / key / 'process.json', current_boot))
                or alive(current_record(runtime / key / 'worker.json', current_boot))
                for key in NAMES)
            if observed_session or action in ('start', 'stop', 'auto_start'):
                auto_allowed = action == 'auto_start' and not observed_session
                write_json(session_path, dict(boot_id=current_boot, at=time.time(),
                           reason='existing_session' if observed_session else action))
    for key in NAMES:
        directory = runtime / key
        directory.mkdir(exist_ok=True)
        try:
            with (directory / 'launch.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                marker_path = directory / 'autostart.json'
                marker = read_json(marker_path)
                handled = bool(current_boot and marker.get('boot_id') == current_boot)
                if action == 'stop' and key == selected:
                    # 先记录人工停止意图，停止请求的回包丢失也不能触发自动拉起。
                    write_json(marker_path, dict(boot_id=current_boot, reason='operator_stopped', at=time.time()))
                    result[key] = stop_program(directory, key, uid)
                    continue
                meta = current_record(directory / 'process.json', current_boot)
                pending = current_record(directory / 'worker.json', current_boot)
                # PID 与启动时钟可能跨重启复用，旧版无 boot_id 的记录也重新扫描确认。
                found = existing(key, uid) if not alive(meta) else {}
                if found:
                    write_json(directory / 'process.json', found)
                    meta = found
                observed = bool(alive(meta) or alive(pending) or health[key]['present'])
                if current_boot and not handled and (observed or meta.get('stopped_by_operator') or key in skip):
                    marker = dict(boot_id=current_boot, reason='observed' if observed else 'operator_stopped', at=time.time())
                    write_json(marker_path, marker)
                    handled = True
                should_start = (selected is None or selected == key) and (
                    action == 'start' or (auto_allowed and not handled and key not in skip))
                if should_start:
                    # 启动意图必须先持久化。在 SSH 超时、回包丢失或地面重开时只查询，
                    # 不把同次开机的崩溃/退出当成新的开机事件。
                    write_json(marker_path, dict(boot_id=current_boot, reason='start_requested', at=time.time()))
                if health[key]['ready'] and not meta:
                    meta = {'borrowed': True, 'node_only': True, 'boot_id': current_boot}
                    write_json(directory / 'process.json', meta)
                    with (directory / 'console.log').open('ab') as log:
                        log.write(('复用已经就绪的%s ROS 节点；原终端历史输出无法追溯。\n' % NAMES[key]).encode())
                if should_start and not alive(meta) and not alive(pending) and not health[key]['ready']:
                    if health[key]['present'] and not found:
                        message = '已有部分 ROS 节点运行，暂不重复启动：' + '、'.join(health[key]['present'])
                        with (directory / 'console.log').open('ab') as log:
                            log.write((message + '\n').encode())
                        info = status(directory, payload.get('offsets', {}).get(key, 0), payload.get('latest_log', False))
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
                        write_json(directory / 'worker.json', {'pid': process.pid, 'stamp': stamp(process.pid), 'boot_id': current_boot})
                        # 启动锁覆盖 worker 写入 child PID 的时间，防止并发确认重复启动。
                        until = time.monotonic() + 2
                        while time.monotonic() < until and process.poll() is None:
                            new = read_json(directory / 'process.json')
                            if new != meta and new:
                                break
                            time.sleep(.03)
                info = status(directory, payload.get('offsets', {}).get(key, 0), payload.get('latest_log', False))
                result[key] = apply_health(info, health[key], read_json(directory / 'process.json'))
        except Exception as error:
            result[key] = dict(state='error', detail='程序操作失败：%s' % error)
    session = current_record(session_path, current_boot)
    result['_lifecycle'] = dict(boot_id=current_boot, auto_start_reason=session.get('reason', ''),
                               auto_start_pending=list(NAMES) if current_boot and not session else [])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--worker':
    worker(sys.argv[2], sys.argv[3])
