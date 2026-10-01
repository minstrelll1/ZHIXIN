"""发布端统一排序的比赛已用时间；跨电脑传递时长，不依赖系统时钟一致。"""
import math
import threading
import time
import uuid


class CompetitionClock:
    def __init__(self, monotonic=time.monotonic):
        self._now = monotonic
        self._lock = threading.RLock()
        self._state = None
        self._anchor = 0.
        self._seen = 0.
        self._authority = False
        self._retired = set()
        self._requests = {}

    def start(self, terminal_id, elapsed_seconds=0.):
        with self._lock:
            if self._authority:
                return self.snapshot()
            self._authority = True
            self._state = dict(session_id=uuid.uuid4().hex, publisher_terminal_id=terminal_id,
                               revision=0, elapsed_seconds=max(0., elapsed_seconds), updated_by=terminal_id)
            self._anchor = self._seen = self._now()
            self._requests.clear()
            return self.snapshot()

    def reset(self):
        with self._lock:
            self._authority = False
            self._state = None
            self._requests.clear()

    def snapshot(self):
        with self._lock:
            if self._state is None:
                return dict(running=False, is_authority=False, elapsed_seconds=0., synchronized=False)
            now = self._now()
            return dict(self._state, running=True, is_authority=self._authority,
                        elapsed_seconds=self._state['elapsed_seconds'] + max(0., now-self._anchor),
                        synchronized=self._authority or now-self._seen <= 3.,
                        sync_age_seconds=0. if self._authority else max(0., now-self._seen))

    def edit(self, seconds, terminal_id, session_id, request_id):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 366*86400:
            raise ValueError('比赛时间须为 0～366 天范围内的有效秒数')
        if type(terminal_id) is not int or terminal_id not in range(1, 7):
            raise ValueError('修改终端编号无效')
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('比赛计时修改请求编号无效')
        with self._lock:
            if not self._authority or not self._state:
                raise ValueError('请等待任务发布端进入并启动比赛计时')
            if session_id != self._state['session_id']:
                raise ValueError('比赛计时会话已变化，请刷新当前时间后重新修改')
            key = (terminal_id, request_id)
            if key in self._requests:
                if self._requests[key] != seconds:
                    raise ValueError('重复请求的比赛时间不一致')
                return self.snapshot()
            self._state.update(elapsed_seconds=float(seconds), updated_by=terminal_id,
                               revision=self._state['revision']+1)
            self._anchor = self._now()
            self._requests[key] = seconds
            if len(self._requests) > 256:
                del self._requests[next(iter(self._requests))]
            return self.snapshot()

    def accept(self, state, source_id, roundtrip_seconds=0.):
        """只接收发布端本人的快照；同会话旧版本不能覆盖新的修改。"""
        if not isinstance(state, dict) or not state.get('running') or not state.get('is_authority'):
            return False
        if state.get('publisher_terminal_id') != source_id or type(state.get('revision')) is not int or state['revision'] < 0:
            return False
        if type(state.get('updated_by')) is not int or state['updated_by'] not in range(1,7):
            return False
        seconds, session = state.get('elapsed_seconds'), state.get('session_id')
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0 or not isinstance(session, str) or not session:
            return False
        with self._lock:
            if self._authority or session in self._retired:
                return False
            if self._state:
                previous = self._state
                if previous['session_id'] == session and state['revision'] < previous['revision']:
                    return False
                if previous['session_id'] != session:
                    self._retired.add(previous['session_id'])
            # 以半个往返耗时估计传输延迟；不使用两台电脑的 wall clock 差。
            elapsed = float(seconds) + max(0., float(roundtrip_seconds))/2
            if self._state and session == self._state['session_id'] and state['revision'] == self._state['revision']:
                elapsed = max(elapsed, self.snapshot()['elapsed_seconds'])
            self._state = {key: state[key] for key in ('session_id','publisher_terminal_id','revision','updated_by')}
            self._state['elapsed_seconds'] = elapsed
            self._anchor = self._seen = self._now()
            return True
