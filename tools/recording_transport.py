"""飞行采集的断网恢复：远端作业独立运行，SSH 重连只查询同一作业。"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


# 此段在 Ubuntu 的 python3 中执行；不依赖竞赛程序或 ROS 消息包。
REMOTE_WORKER = r'''
import base64, fcntl, hashlib, json, os, re, shlex, subprocess
from pathlib import Path

def rpc(request):
    if not re.fullmatch(r'/home/[^/]+/flight_records/uav[1-6]_\d{8}_\d{6}', request['directory']):
        raise ValueError('无效的采集目录')
    directory = Path(request['directory'])
    if request['action'] == 'files':
        return json.loads((directory / '.ground_jobs' / 'download_manifest.json').read_text())
    if request['action'] == 'build_manifest':
        if list(directory.glob('*.bag.active')):
            raise RuntimeError('rosbag 尚未完成，不能复制未关闭的记录')
        manifest = {}
        manifest_path = directory / 'bag_sha256.txt'
        if manifest_path.exists():
            for line in manifest_path.read_text().splitlines():
                match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(.+)', line)
                if match:
                    manifest[match[2]] = match[1].lower()
        files = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.is_symlink() or path.name.startswith('.'):
                continue
            digest = manifest.get(path.name)
            if not digest:
                h = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(block)
                digest = h.hexdigest()
            files.append(dict(name=path.name, size=path.stat().st_size, sha256=digest))
        result = dict(files=files)
        (directory / '.ground_jobs').mkdir(exist_ok=True)
        temporary = directory / '.ground_jobs' / 'download_manifest.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=True))
        temporary.replace(directory / '.ground_jobs' / 'download_manifest.json')
        return result
    job_id = request['job_id']
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('无效的采集作业编号')
    job = directory / '.ground_jobs' / job_id
    job.mkdir(parents=True, exist_ok=True)
    with (job / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        done, pid_path, log_path = job / 'done', job / 'pid', job / 'output.log'
        if not pid_path.exists() and not done.exists():
            script = job / 'script.sh'
            script.write_bytes(base64.b64decode(request['script']))
            # 任务不依附 SSH 会话；断电期间继续完成停止/校验/导出，重连不重复执行。
            shell = ('bash ' + shlex.quote(str(script)) + '; code=$?; '
                     + "printf '%s' \"$code\" > " + shlex.quote(str(job / 'done.tmp'))
                     + '; mv ' + shlex.quote(str(job / 'done.tmp')) + ' ' + shlex.quote(str(done)))
            with log_path.open('wb') as log:
                process = subprocess.Popen(['bash', '-c', shell], stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
            pid_path.write_text(str(process.pid))
        offset = max(0, int(request.get('offset', 0)))
        with log_path.open('rb') as log:
            log.seek(offset)
            data = log.read(65536)
        offset += len(data)
        complete = done.exists() and offset >= log_path.stat().st_size
        if not done.exists():
            try:
                os.kill(int(pid_path.read_text()), 0)
            except ProcessLookupError:
                raise RuntimeError('机载采集作业意外结束，原始数据保留在 ' + str(directory))
        return dict(done=complete, code=int(done.read_text()) if complete else None,
                    output=base64.b64encode(data).decode('ascii'), offset=offset)
'''


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def retryable(returncode, detail):
    text = detail.lower()
    if any(word in text for word in ('permission denied', 'host key verification failed',
                                     'remote host identification has changed', 'no such file')):
        return False
    return returncode == 255 or any(word in text for word in (
        'timed out', 'connection reset', 'connection closed', 'broken pipe',
        'no route to host', 'network is unreachable', 'connection refused', 'lost connection'))


class RecordingTransport:
    def __init__(self, host, directory, identity=None, retry_seconds=5.0):
        self.host, self.directory = host, directory
        self.options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                        '-o', 'ServerAliveInterval=3', '-o', 'ServerAliveCountMax=2']
        if identity:
            self.options += ['-i', identity, '-o', 'IdentitiesOnly=yes']
        self.retry_seconds = retry_seconds
        self.disconnected = False

    def _lost(self):
        if not self.disconnected:
            print('采集链路中断，等待自动重连；机载记录和作业继续保留，不会重新开始采集。', flush=True)
        self.disconnected = True
        time.sleep(self.retry_seconds)

    def _restored(self):
        if self.disconnected:
            print('采集链路已恢复，继续本次停止、整理或下载。', flush=True)
        self.disconnected = False

    def request(self, payload):
        payload = dict(payload, directory=self.directory)
        source = REMOTE_WORKER + '\nprint(json.dumps(rpc(' + repr(payload) + '), ensure_ascii=True))\n'
        encoded = base64.b64encode(source.encode('utf-8')).decode('ascii')
        program = "import base64; exec(compile(base64.b64decode(%r), '<recording>', 'exec'))" % encoded
        while True:
            try:
                result = subprocess.run(['ssh', *self.options, self.host, 'python3 -'],
                    input=program.encode('ascii'), capture_output=True, timeout=20,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                self._lost()
                continue
            if result.returncode:
                detail = result.stderr.decode('utf-8', errors='replace').strip()
                if retryable(result.returncode, detail):
                    self._lost()
                    continue
                raise RuntimeError('机载采集操作失败：' + detail)
            self._restored()
            return json.loads(result.stdout.decode('utf-8'))

    def run_job(self, script):
        # 同一次调用重试使用同一个编号，SSH 丢失响应也不会重复启动 rosbag。
        payload = dict(action='job', job_id=uuid.uuid4().hex, script=script, offset=0)
        while True:
            state = self.request(payload)
            output = base64.b64decode(state['output'])
            if output:
                sys.stdout.buffer.write(output)
                sys.stdout.buffer.flush()
            payload['offset'] = state['offset']
            if state['done']:
                return int(state['code'])
            time.sleep(1)

    def copy(self, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        # 大文件哈希也在独立作业中计算；SSH 的短请求超时不能反复中止它。
        source = REMOTE_WORKER + '\nrpc(' + repr(dict(action='build_manifest', directory=self.directory)) + ')\n'
        program = "python3 - <<'RECORDING_MANIFEST_PY'\n" + source + '\nRECORDING_MANIFEST_PY\n'
        if self.run_job(base64.b64encode(program.encode('utf-8')).decode('ascii')):
            raise RuntimeError('采集文件清单生成失败；机载原文件保留')
        files = self.request(dict(action='files'))['files']
        for item in files:
            name = item['name']
            if Path(name).name != name or name in ('', '.', '..') or '/' in name or '\\' in name:
                raise ValueError('机载返回了无效的采集文件名')
            target = destination / name
            if (target.is_file() and target.stat().st_size == item['size']
                    and file_hash(target) == item['sha256']):
                continue
            partial = destination / ('.' + name + '.download')
            print('正在下载并校验：' + name, flush=True)
            while True:
                result = subprocess.run(['scp', *self.options, self.host + ':' + self.directory + '/' + name,
                                         str(partial)], capture_output=True,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if result.returncode:
                    detail = result.stderr.decode('utf-8', errors='replace').strip()
                    if retryable(result.returncode, detail):
                        self._lost()
                        continue
                    raise RuntimeError('采集文件下载失败，机载原文件仍保留：' + detail)
                self._restored()
                if partial.stat().st_size != item['size'] or file_hash(partial) != item['sha256']:
                    raise RuntimeError('采集文件大小或 SHA-256 校验失败：' + name)
                os.replace(partial, target)
                break


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--identity')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--script-base64')
    actions.add_argument('--copy-to')
    args = parser.parse_args()
    transport = RecordingTransport(args.host, args.directory, args.identity)
    try:
        if args.copy_to:
            transport.copy(args.copy_to)
            return 0
        return transport.run_job(args.script_base64)
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
