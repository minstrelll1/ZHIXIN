"""只读留存飞控/厂商提示，不发布任何控制指令。"""
from collections import deque
import importlib
import json
import math
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
import uuid


class FlightDiagnostics:
    def __init__(self, clock=time.monotonic, logger=None):
        self.clock = clock
        self.logger = logger
        self.lock = threading.Lock()
        self.events = deque(maxlen=40)
        self.last_logged = {}
        self.subscribers = []
        self.instance_id = uuid.uuid4().hex
        self.alerts = deque(maxlen=32)
        self.sequence = 0
        self.samples = {}
        self.last_snapshot = None
        self.last_snapshot_at = float('-inf')
        self.last_wire_signature = None
        self.last_wire_at = float('-inf')
        self.storage = None
        self.context = {}

    def configure_storage(self, directory, boot_id, uav_id):
        """机地断连时仍在机载留证；不同开机不覆盖，单次开机最多约25MiB。"""
        self.context = dict(boot_id=boot_id, uav_id=uav_id, instance_id=self.instance_id)
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            self.storage = RotatingFileHandler(directory/'diagnostics.jsonl',
                maxBytes=5*1024*1024, backupCount=4, encoding='utf-8')
            self.storage.setFormatter(logging.Formatter('%(message)s'))
            self._persist('diagnostic_started', {})
        except Exception as error:
            logging.getLogger(__name__).warning('机载诊断文件不可用，业务继续：%s', error)

    def _persist(self, event, data):
        try:
            if self.storage:
                payload = dict(self.context, event=event, recorded_wall_time=time.time(),
                               recorded_monotonic=self.clock(), data=data)
                self.storage.handle(logging.LogRecord('flight_diagnostics', logging.INFO, '', 0,
                    json.dumps(payload, ensure_ascii=False, allow_nan=False), (), None))
        except Exception as error:
            logging.getLogger(__name__).warning('机载诊断写入失败，业务继续：%s', error)

    def close(self):
        if self.storage:
            self.storage.close()

    def record(self, source, severity, text, source_stamp=None):
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
                self.sequence += 1
                self.alerts.append(dict(sequence=self.sequence, source=source, severity=int(severity),
                                        text=text, source_stamp=source_stamp,
                                        received_monotonic=now, received_wall_time=time.time()))
        if emit:
            self._persist('flight_alert', dict(source=source, severity=int(severity), text=text, source_stamp=source_stamp))
            if self.logger:
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

    def _sample(self, name, message, fields):
        try:
            sample = {}
            for key, path in fields.items():
                value = message
                for part in path.split('.'):
                    value = getattr(value, part, None)
                sample[key] = None if isinstance(value, float) and not math.isfinite(value) else value
            stamp = getattr(getattr(message, 'header', None), 'stamp', None)
            sample['source_stamp'] = stamp.to_sec() if stamp is not None else None
            sample['received_wall_time'] = time.time()
            with self.lock:
                self.samples[name] = (self.clock(), sample)
        except Exception as error:
            self.record('diagnostic', 4, '诊断采样失败：%s: %s' % (name, error))

    def localization_snapshot(self, state, control, state_age, control_age, gps, home_reason):
        """诊断副本；不推断根因、不改变 odom_valid，也不写飞控参数。"""
        now = self.clock()
        with self.lock:
            samples = {name: dict(value, age_seconds=max(0, now-at)) for name, (at, value) in self.samples.items()}
            alerts = list(self.alerts)[-12:]
        fields = {name: getattr(state, name, None) for name in
                  ('connected', 'armed', 'odom_valid', 'location_source', 'gps_status', 'gps_num', 'mode')}
        fields.update(control_state=getattr(control, 'control_state', None),
                      failsafe=getattr(control, 'failsafe', None))
        result = dict(instance_id=self.instance_id, sampled_wall_time=time.time(), sampled_monotonic=now,
                      state=fields, state_age_seconds=state_age, control_age_seconds=control_age,
                      gps_position=gps, boot_home_reason=home_reason, samples=samples, alerts=alerts)
        def clean(value):
            if isinstance(value, float) and not math.isfinite(value):
                return None
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            return value
        result = clean(result)
        signature = json.dumps((fields, home_reason), sort_keys=True)
        if signature != self.last_snapshot or now-self.last_snapshot_at >= 5:
            self.last_snapshot, self.last_snapshot_at = signature, now
            self._persist('localization_snapshot', result)
            if self.logger:
                self.logger('定位诊断快照（只读）：%s', json.dumps(result, ensure_ascii=False))
        return result

    def wire_snapshot(self, snapshot):
        """附在原有遥测上，稳定时每5秒一次；不重复以5Hz传输整批诊断。"""
        state = {key: value for key, value in snapshot['state'].items() if key != 'gps_num'}
        signature = json.dumps((state, snapshot['boot_home_reason'],
                               [item['sequence'] for item in snapshot['alerts']]), sort_keys=True)
        now = self.clock()
        if signature == self.last_wire_signature and now-self.last_wire_at < 5:
            return None
        self.last_wire_signature, self.last_wire_at = signature, now
        return snapshot

    def subscribe(self, rospy, prefix):
        from mavros_msgs.msg import StatusText
        from prometheus_msgs.msg import TextInfo

        def px4(message):
            stamp = getattr(getattr(message, 'header', None), 'stamp', None)
            self.record('PX4', message.severity, message.text, stamp.to_sec() if stamp is not None else None)

        def prometheus(message):
            # TextInfo 为 INFO=0/WARN=1/ERROR=2；转为 MAVLink 等级用于筛选。
            level = {0: 6, 1: 4, 2: 3, 3: 2}.get(int(message.MessageType), 6)
            stamp = getattr(getattr(message, 'header', None), 'stamp', None)
            self.record('Prometheus', level, message.Message, stamp.to_sec() if stamp is not None else None)

        self.subscribers = [
            rospy.Subscriber(prefix + '/mavros/statustext/recv', StatusText, px4, queue_size=50),
            rospy.Subscriber(prefix + '/prometheus/text_info', TextInfo, prometheus, queue_size=50),
        ]
        optional = [
            ('gps_raw', 'mavros_msgs.msg', 'GPSRAW', '/mavros/gpsstatus/gps1/raw',
             {key: key for key in ('fix_type', 'satellites_visible', 'eph', 'epv', 'lat', 'lon', 'alt')}),
            ('gps_fix', 'sensor_msgs.msg', 'NavSatFix', '/mavros/global_position/global',
             dict(status='status.status', service='status.service', latitude='latitude', longitude='longitude',
                  altitude='altitude', covariance='position_covariance', covariance_type='position_covariance_type')),
            ('estimator', 'mavros_msgs.msg', 'EstimatorStatus', '/mavros/estimator_status',
             {key: key for key in ('flags', 'vel_ratio', 'pos_horiz_ratio', 'pos_vert_ratio', 'mag_ratio',
                                   'hagl_ratio', 'tas_ratio', 'pos_horiz_accuracy', 'pos_vert_accuracy')}),
            ('local_pose', 'geometry_msgs.msg', 'PoseStamped', '/mavros/local_position/pose',
             dict(x='pose.position.x', y='pose.position.y', z='pose.position.z')),
        ]
        for name, package, typename, topic, fields in optional:
            try:
                message_type = getattr(importlib.import_module(package), typename)
                self.subscribers.append(rospy.Subscriber(prefix+topic, message_type,
                    lambda message, n=name, f=fields: self._sample(n, message, f), queue_size=1))
            except (ImportError, AttributeError) as error:
                self.record('diagnostic', 4, '只读诊断订阅不可用：%s: %s' % (topic, error))
