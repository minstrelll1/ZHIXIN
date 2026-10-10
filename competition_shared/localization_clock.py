"""只读保存定位时刻与本次开机单调时钟的关系；绝不调整系统时间。"""
import re
import threading
import uuid
from pathlib import Path

NANOSECONDS = 1_000_000_000
JUMP_TOLERANCE_NS = 250_000_000
EVENT_TOLERANCE_NS = 50_000_000


def read_boot_id():
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()
        return value if re.fullmatch(r'[0-9a-fA-F-]{36}', value) else ''
    except (OSError, ValueError):
        return ''


def stamp_nanoseconds(value):
    if not isinstance(value, dict):
        return None
    sec, nsec = value.get('secs'), value.get('nsecs')
    if (type(sec) is not int or type(nsec) is not int or sec < 0
            or not 0 <= nsec < NANOSECONDS or sec == nsec == 0):
        return None
    return sec * NANOSECONDS + nsec


class SourceClockTracker:
    """显式输入时钟样本，便于无 ROS/无人机的确定性测试。"""
    def __init__(self, boot_id=None):
        self.boot_id = read_boot_id() if boot_id is None else str(boot_id)
        self.lock = threading.RLock()
        self.instance = uuid.uuid4().hex
        self.segment_number = 0
        self.segment_start = None
        self.last = None
        self.retired_ros_ranges = []

    def _start_segment(self, sample, cause):
        if self.last is not None and self.segment_start is not None and not self.last.get('invalid_reason'):
            self.retired_ros_ranges.append((self.segment_start['source_ros_ns'], self.last['source_ros_ns']))
        self.segment_number += 1
        self.segment_start = dict(sample)
        sample['segment_cause'] = cause

    def observe(self, monotonic_ns, system_ns, ros_ns, use_sim_time=False, sampling_uncertainty_ns=0):
        with self.lock:
            sample = dict(sample_monotonic_ns=monotonic_ns, source_system_ns=system_ns,
                          source_ros_ns=ros_ns, use_sim_time=bool(use_sim_time),
                          sampling_uncertainty_ns=sampling_uncertainty_ns)
            reason = ''
            if not self.boot_id:
                reason = 'unknown_boot'
            elif use_sim_time:
                reason = 'simulation_time'
            elif any(type(value) is not int or value <= 0 for value in (monotonic_ns, system_ns, ros_ns)):
                reason = 'invalid_source_clock'
            elif type(sampling_uncertainty_ns) is not int or not 0 <= sampling_uncertainty_ns <= EVENT_TOLERANCE_NS:
                reason = 'slow_clock_sample'
            if reason:
                # 无法观察的间隙不能证明前后时钟连续。
                if not self.last or self.last.get('invalid_reason') != reason:
                    self._start_segment(sample, reason)
                else:
                    sample['segment_cause'] = reason
                sample['invalid_reason'] = reason
                self.last = sample
                return dict(sample)
            cause = 'initial_observation'
            if self.last is not None and not self.last.get('invalid_reason'):
                delta = monotonic_ns - self.last['sample_monotonic_ns']
                if delta < 0:
                    cause = 'monotonic_rollback'
                elif abs(ros_ns - self.last['source_ros_ns'] - delta) > JUMP_TOLERANCE_NS:
                    cause = 'ros_clock_jump'
                elif abs(system_ns - self.last['source_system_ns'] - delta) > JUMP_TOLERANCE_NS:
                    cause = 'system_clock_jump'
                else:
                    cause = ''
            if cause or self.segment_start is None:
                self._start_segment(sample, cause)
            else:
                sample['segment_cause'] = self.last.get('segment_cause', 'initial_observation')
            self.last = sample
            return dict(sample)

    def context(self, localization_time):
        with self.lock:
            source = self.last or {}
            start = self.segment_start or {}
            output = dict(schema_version=1, boot_id=self.boot_id,
                          segment_id='%s:%s' % (self.instance, self.segment_number),
                          event_monotonic_ns=None, mapping_valid=False, reason='no_clock_sample',
                          detail='尚无机载时钟观察记录')
            output.update(source)
            output['segment_start_monotonic_ns'] = start.get('sample_monotonic_ns')
            output['segment_start_ros_ns'] = start.get('source_ros_ns')
            if not source:
                return output
            reason = source.get('invalid_reason', '')
            event_ns = stamp_nanoseconds(localization_time)
            if not reason and event_ns is None:
                reason = 'invalid_localization_time'
            if not reason and event_ns < start['source_ros_ns'] - EVENT_TOLERANCE_NS:
                reason = 'before_observed_segment'
            if not reason and event_ns > source['source_ros_ns'] + EVENT_TOLERANCE_NS:
                reason = 'future_localization_time'
            if not reason and any(type(a) is int and type(b) is int and min(a, b) <= event_ns <= max(a, b)
                                  for a, b in self.retired_ros_ranges):
                reason = 'ambiguous_previous_segment'
            estimated = None if reason else source['sample_monotonic_ns'] + event_ns - source['source_ros_ns']
            if not reason and estimated < 0:
                reason = 'invalid_event_monotonic'
            descriptions = dict(unknown_boot='机载开机标识不可用', simulation_time='ROS 使用仿真时间',
                invalid_source_clock='机载时间样本无效', slow_clock_sample='读取时钟耗时过长',
                invalid_localization_time='原始 localization_time 无效',
                before_observed_segment='定位时刻早于当前已观察的连续时钟段',
                future_localization_time='定位时刻超过当前 ROS 时间',
                ambiguous_previous_segment='定位时刻与跳变前的时钟段重叠，无法确认归属',
                invalid_event_monotonic='定位时刻不能对应本次开机单调时间')
            if reason:
                output.update(reason=reason, detail=descriptions.get(reason, reason))
            else:
                output.update(event_monotonic_ns=estimated, mapping_valid=True, reason='ok',
                              detail='定位时刻已关联本次开机的连续单调时钟；等待发布端时间参考')
            return output
