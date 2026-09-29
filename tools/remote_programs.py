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


def command_for(key, uid, model):
    if key == "onboard":
        return "exec bash ./tools/start_onboard_stack.sh --model %s --expect-uav-id %d --direct --enable-motion" % (model, uid)
    if key == "detection":
        return "source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash && exec roslaunch spirecv_ros uav_yolo26_botsort_geolocation.launch uav_id:=%d" % uid
    return "source ~/recon_ws/devel/setup.bash && exec roslaunch px4_north_camera p600_gx40_position_pid_reconnaissance.launch uav_id:=%d flight_mode:=outdoor_small_range" % uid


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
    unhealthy = unhealthy and not meta.get('borrowed')
    state = 'error' if unhealthy else ('running' if live else ('stopped' if meta else 'idle'))
    detail = '进程运行中' if live else '程序尚未启动或已停止'
    if unhealthy:
        detail = '检测到子进程异常，请查看打印内容'
    if meta.get('borrowed') and live:
        detail = '复用原有进程；原终端历史输出无法追溯'
    return dict(state=state, detail=detail, pid=meta.get('pid'), exit_code=meta.get('exit_code'),
                log=base64.b64encode(data).decode(), offset=offset + len(data), size=size)


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
    for key in NAMES:
        directory = runtime / key
        directory.mkdir(exist_ok=True)
        try:
            with (directory / 'launch.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                meta = read_json(directory / 'process.json')
                pending = read_json(directory / 'worker.json')
                found = existing(key, uid) if not alive(meta) else {}
                if found and payload.get('action') != 'start':
                    write_json(directory / 'process.json', found)
                if payload.get('action') == 'start' and not alive(meta) and not alive(pending):
                    if found:
                        write_json(directory / 'process.json', found)
                        with (directory / 'console.log').open('ab') as log:
                            log.write(('复用已运行的%s，PID=%s；此前终端输出无法追溯。\n' % (NAMES[key], found['pid'])).encode())
                    else:
                        helper = directory / 'runner.py'
                        helper.write_text(PROGRAM_SOURCE, encoding='utf-8')
                        with (directory / 'console.log').open('ab') as log:
                            process = subprocess.Popen([sys.executable, str(helper), '--worker', str(directory), command_for(key, uid, model)],
                                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
                        write_json(directory / 'worker.json', {'pid': process.pid, 'stamp': stamp(process.pid)})
                        # 启动锁覆盖 worker 写入 child PID 的时间，防止并发确认重复启动。
                        until = time.monotonic() + 2
                        while time.monotonic() < until and process.poll() is None:
                            new = read_json(directory / 'process.json')
                            if new != meta and new:
                                break
                            time.sleep(.03)
                result[key] = status(directory, payload.get('offsets', {}).get(key, 0))
        except Exception as error:
            result[key] = dict(state='error', detail='启动或读取失败：%s' % error)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--worker':
    worker(sys.argv[2], sys.argv[3])
