"""只读留存飞控/厂商提示，不发布任何控制指令。"""
from collections import deque
import threading
import time


class FlightDiagnostics:
    def __init__(self, clock=time.monotonic, logger=None):
        self.clock = clock
        self.logger = logger
        self.lock = threading.Lock()
        self.events = deque(maxlen=40)
        self.last_logged = {}
        self.subscribers = []

    def record(self, source, severity, text):
        text = str(text).strip().replace('\x00', '')[:600]
        if not text:
            return
        now = self.clock()
        key = (source, int(severity), text)
        with self.lock:
            # 重复消息仍更新缓存时间，但限制重复日志量。
            self.events.append((now, source, int(severity), text))
            last = self.last_logged.get(key, float('-inf'))
            emit = now - last >= 5
            if emit:
                if len(self.last_logged) >= 100:
                    self.last_logged.clear()
                self.last_logged[key] = now
        if emit and self.logger:
            self.logger('飞控诊断[%s][级别%d] %s', source, severity, text)

    def recent_warnings(self, max_age=15):
        now = self.clock()
        with self.lock:
            events = list(self.events)
        result = []
        for stamp, source, severity, text in reversed(events):
            if 0 <= now - stamp <= max_age and severity <= 4:
                item = '%s: %s' % (source, text)
                if item not in result:
                    result.append(item)
            if len(result) == 3:
                break
        return '；'.join(result)

    def subscribe(self, rospy, prefix):
        from mavros_msgs.msg import StatusText
        from prometheus_msgs.msg import TextInfo

        def px4(message):
            self.record('PX4', message.severity, message.text)

        def prometheus(message):
            # TextInfo 为 INFO=0/WARN=1/ERROR=2；转为 MAVLink 等级用于筛选。
            level = {0: 6, 1: 4, 2: 3, 3: 2}.get(int(message.MessageType), 6)
            self.record('Prometheus', level, message.Message)

        self.subscribers = [
            rospy.Subscriber(prefix + '/mavros/statustext/recv', StatusText, px4, queue_size=50),
            rospy.Subscriber(prefix + '/prometheus/text_info', TextInfo, prometheus, queue_size=50),
        ]
