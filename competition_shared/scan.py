"""扫描完成必须属于当前请求；单纯经过悬停时间不等于扫描完成。"""
class ScanSession:
    def __init__(self, request_id, duration, timeout, now, require_ack=True):
        self.request_id = request_id
        self.duration = float(duration)
        self.timeout = max(float(timeout), self.duration)
        self.started = now
        self.require_ack = require_ack
        self.acknowledged = False
        self.failed = False

    def accept(self, message):
        if message.get('request_id') != self.request_id:
            return
        if message.get('state') == 'completed':
            self.acknowledged = True
        if message.get('state') == 'failed':
            self.failed = True

    def state(self, now):
        if self.failed:
            return 'failed'
        elapsed = now - self.started
        if elapsed >= self.duration and (self.acknowledged or not self.require_ack):
            return 'completed'
        if elapsed >= self.timeout:
            return 'timeout'
        return 'waiting'
