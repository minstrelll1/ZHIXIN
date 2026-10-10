"""只读采样机载单调时钟，以发布端 Windows 时间标定目标事件时间。"""
import copy
import json
import math
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class TimeReferenceCollector:
    def __init__(self, path, publisher_terminal_id, enabled, connected_uavs, probe,
                 audit=None, on_update=None, interval_seconds=30.,
                 monotonic=time.monotonic, wall_clock=time.time):
        self.path = Path(path)
        self.publisher_terminal_id = int(publisher_terminal_id)
        self.enabled = enabled
        self.connected_uavs = connected_uavs
        self.probe = probe
        self.audit = audit
        self.on_update = on_update
        self.interval_seconds = max(1., float(interval_seconds))
        self._mono, self._wall = monotonic, wall_clock
        self._lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._references, self._states = {}, {}
        self._last_attempt = {}
        self._load_error = ''
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('schema_version') != 1 or not isinstance(value.get('references'), dict):
                raise ValueError('时间参考文件格式不兼容')
            self._references = value['references']
        except (OSError, ValueError, TypeError, AttributeError) as error:
            # 损坏的历史参考不可用空表覆盖，以免丢失离线目标的事件证据。
            self._load_error = str(error)

    def _record(self, event, **fields):
        if self.audit:
            try:
                self.audit.record(event, **fields)
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='target-time-reference', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.)

    def wake(self):
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.collect_once(due_only=True)
            except Exception as error:
                self._record('目标事件时间参考采集异常', error=str(error))
            # 新连接无需等待完整周期；已采样飞机按30秒周期刷新。
            self._wake.wait(min(3., self.interval_seconds))

    def snapshot(self):
        with self._lock:
            return dict(enabled=bool(self.enabled()), running=bool(self._thread and self._thread.is_alive()),
                        source='publisher_windows', publisher_terminal_id=self.publisher_terminal_id,
                        reference_count=len(self._references), file=str(self.path),
                        error=self._load_error, by_uav=copy.deepcopy(self._states))

    def _sample(self, uav_id):
        samples, errors, boots = [], [], set()
        batch_mono, batch_wall = self._mono(), self._wall()
        for _ in range(3):
            if self._stop.is_set() or not self.enabled():
                raise RuntimeError('时间参考采集已停止或本机不再是任务发布端')
            start, wall = self._mono(), self._wall()
            try:
                remote = self.probe(uav_id)
                end, wall_end = self._mono(), self._wall()
                elapsed = end - start
                if (abs((wall_end - wall) - elapsed) > .1
                        or abs((wall_end - batch_wall) - (end - batch_mono)) > .1):
                    # 整批失效，避免选择调钟前后来自不同基准的最短样本。
                    raise ClockJump('采样期间发布端 Windows 时间发生跳变，等待下一轮重新采样')
                if not 0 <= elapsed <= 2.:
                    raise ValueError('只读时间采样往返超过 2 秒')
                boot = str(uuid.UUID(str(remote['boot_id'])))
                boots.add(boot)
                if len(boots) != 1:
                    raise BootChanged('采样期间机载电脑重新开机，等待下一轮重新采样')
                mono = float(remote['remote_monotonic'])
                remote_wall = float(remote['remote_wall'])
                if not math.isfinite(mono) or mono < 0 or not math.isfinite(remote_wall) or remote_wall < 0:
                    raise ValueError('机载只读时钟采样无效')
                ground_epoch = wall + elapsed / 2.
                if not 1704067200 <= ground_epoch < 4102444800:
                    raise ValueError('发布端 Windows 日期不在 2024～2099 年，请先核对电脑时间')
                samples.append(dict(boot_id=boot, uav_id=uav_id, ground_epoch=ground_epoch,
                                    remote_monotonic=mono, uncertainty_sec=elapsed / 2.,
                                    sampled_at=wall_end, source='publisher_windows',
                                    publisher_terminal_id=self.publisher_terminal_id,
                                    transport=str(remote.get('transport', 'legacy'))))
            except (ClockJump, BootChanged):
                raise
            except Exception as error:
                errors.append(str(error) or error.__class__.__name__)
        # 探针抛错也可能跨越 Windows 调钟，选用早期成功样本前复核整批。
        end_mono, end_wall = self._mono(), self._wall()
        if abs((end_wall - batch_wall) - (end_mono - batch_mono)) > .1:
            raise ClockJump('采样期间发布端 Windows 时间发生跳变，等待下一轮重新采样')
        if not samples:
            raise RuntimeError('；'.join(dict.fromkeys(errors)))
        return min(samples, key=lambda item: item['uncertainty_sec'])

    def _save(self, reference):
        with self._lock:
            if self._load_error:
                raise RuntimeError('原时间参考文件读取失败，未覆盖历史文件：' + self._load_error)
            boot = reference['boot_id']
            previous = self._references.get(boot)
            if previous and previous.get('uav_id') != reference['uav_id']:
                raise ValueError('同一开机标识对应不同无人机，拒绝覆盖时间参考')
            updated = dict(self._references)
            updated[boot] = reference
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='time_references-', suffix='.json.part', dir=str(self.path.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(dict(schema_version=1, references=updated), stream, ensure_ascii=False,
                              allow_nan=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                # Windows索引/整理线程短暂占用文件时，保留临时文件并有界重试。
                for attempt in range(4):
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if attempt == 3:
                            raise
                        time.sleep(.05 * (attempt + 1))
                self._references = updated
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _collect_uav(self, uav_id):
        try:
            reference = self._sample(uav_id)
            if self._stop.is_set() or not self.enabled():
                return
            self._save(reference)
            state = dict(state='ready', message='已记录机地时差参考，未修改机载系统时间', **reference)
            self._record('目标事件时间参考已更新', **reference)
            if self.on_update:
                try:
                    self.on_update(uav_id, reference)
                except Exception as error:
                    self._record('时间参考更新后成果重整失败', uav_id=uav_id, error=str(error))
        except Exception as error:
            state = dict(state='unavailable', message=str(error), sampled_at=self._wall())
            with self._lock:
                previous = self._states.get(str(uav_id), {})
            if previous.get('message') != state['message']:
                self._record('目标事件时间参考暂不可用', uav_id=uav_id, error=str(error))
        with self._lock:
            self._states[str(uav_id)] = state

    def collect_once(self, due_only=False):
        if self._stop.is_set() or not self.enabled() or not self._cycle_lock.acquire(blocking=False):
            return
        try:
            ids = sorted({int(uid) for uid in self.connected_uavs() if int(uid) in range(1, 7)})
            with self._lock:
                for key, state in self._states.items():
                    if int(key) not in ids:
                        self._last_attempt.pop(int(key), None)
                        self._states[key] = dict(state, state='disconnected', message='飞机未连接，保留历史时间参考')
                if due_only:
                    ids = [uid for uid in ids if self._mono() - self._last_attempt.get(uid, float('-inf')) >=
                           (min(5., self.interval_seconds) if self._states.get(str(uid), {}).get('state') == 'unavailable'
                            else self.interval_seconds)]
                for uid in ids:
                    self._last_attempt[uid] = self._mono()
                    self._states[str(uid)] = dict(state='sampling', message='正在只读采样机地时差')
            if ids:
                with ThreadPoolExecutor(max_workers=len(ids), thread_name_prefix='target-time-probe') as pool:
                    list(pool.map(self._collect_uav, ids))
        finally:
            self._cycle_lock.release()


class ClockJump(RuntimeError):
    pass


class BootChanged(RuntimeError):
    pass
