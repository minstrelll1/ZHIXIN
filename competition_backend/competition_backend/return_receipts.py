"""逐机跟踪人工返航请求及机载回执，不自动重发、不控制飞行。"""
import copy
import threading
import time
import uuid


class ReturnReceiptTracker:
    TIMEOUT_SECONDS = 8.0
    ACK_STATES = frozenset(('accepted', 'forwarded', 'executing', 'already_returning', 'landed', 'rejected', 'failed'))

    def __init__(self, audit=None, clock=time.monotonic):
        self.audit, self.clock = audit, clock
        self._lock = threading.RLock()
        self._requests = {}

    def _record(self, event, **fields):
        if self.audit:
            try:
                self.audit.record(event, **fields)
            except Exception:
                pass  # 诊断落盘异常不能阻止人工返航。

    def send(self, uid, payload, sender):
        uid = int(uid)
        request = dict(payload, request_id=uuid.uuid4().hex)
        entry = dict(uav_id=uid, request_id=request['request_id'], mission_id=request['mission_id'],
                     assignment_checksum=request.get('assignment_checksum', ''), state='waiting',
                     detail='已提交返航请求，等待机载回执', ack_received=False, ack_seq=0,
                     started_monotonic=self.clock())
        with self._lock:
            self._requests[str(uid)] = entry
        self._record('返航请求等待机载回执', **entry)
        try:
            sender(uid, request)
        except Exception as error:
            with self._lock:
                if not entry['ack_received']:
                    entry.update(state='send_failed', detail='发送失败或结果未知：%s；恢复连接后可单独重发' % error)
            self._record('返航请求网络发送异常', uav_id=uid, request_id=request['request_id'], error=str(error))
            raise
        return request['request_id']

    def snapshot(self, telemetry):
        now = self.clock()
        events = []
        with self._lock:
            result = {}
            for key, entry in self._requests.items():
                item = (telemetry or {}).get(key) or (telemetry or {}).get(int(key)) or {}
                if not isinstance(item, dict): item = vars(item)
                ack = item.get('return_ack') or {}
                if (isinstance(ack, dict) and ack.get('request_id') == entry['request_id']
                        and ack.get('mission_id') == entry['mission_id']
                        and type(ack.get('uav_id')) is int and ack['uav_id'] == entry['uav_id']
                        and ack.get('assignment_checksum') == entry['assignment_checksum']
                        and isinstance(ack.get('state'), str) and ack['state'] in self.ACK_STATES
                        and type(ack.get('ack_seq')) is int and ack['ack_seq'] > entry['ack_seq']):
                    entry.update(state=ack['state'], detail=str(ack.get('detail', '')), ack_received=True,
                                 ack_seq=ack['ack_seq'], controller_mode=ack.get('controller_mode'))
                    events.append(('返航机载回执已确认', copy.deepcopy(entry)))
                age = max(0., now-entry['started_monotonic'])
                if entry['state'] == 'waiting' and age >= self.TIMEOUT_SECONDS:
                    entry.update(state='timeout', detail='8秒内未收到机载回执，是否执行尚不确定；可单独勾选重发')
                    events.append(('返航机载回执等待超时', copy.deepcopy(entry)))
                view = {k: copy.deepcopy(v) for k,v in entry.items() if k != 'started_monotonic'}
                view.update(age_seconds=age, timeout_seconds=self.TIMEOUT_SECONDS)
                result[key] = view
        for event, data in events: self._record(event, **data)
        return result
