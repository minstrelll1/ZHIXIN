"""只读时钟证据：模拟时钟跳变/延迟/补传，不连接无人机或写系统时间。"""
import json
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from competition_shared.localization_clock import SourceClockTracker, stamp_nanoseconds
import test_timestamped_detection as base
from test_completed_target_v2 import target

N = 1_000_000_000
BOOT = 'd1594ed0-a710-4630-b90e-536df5423cbf'


def stamp(ns):
    return dict(secs=ns // N, nsecs=ns % N)


class SourceClockTests(unittest.TestCase):
    def tracker(self):
        tracker = SourceClockTracker(BOOT)
        tracker.observe(10*N, 1000*N, 1000*N)
        tracker.observe(15*N, 1005*N, 1005*N)
        return tracker

    def test_past_event_maps_to_monotonic_not_receive_time(self):
        tracker = self.tracker()
        raw = stamp(1002*N + 123456789)
        result = tracker.context(raw)
        self.assertTrue(result['mapping_valid'])
        self.assertEqual(result['event_monotonic_ns'], 12*N + 123456789)
        self.assertEqual(result['sample_monotonic_ns'], 15*N)
        self.assertEqual(raw, dict(secs=1002, nsecs=123456789))
        self.assertEqual(result['boot_id'], BOOT)
        json.dumps(result, allow_nan=False)

    def test_old_and_future_events_outside_segment_are_not_claimed_valid(self):
        tracker = self.tracker()
        for ns, reason in [(1000*N - 50000001, 'before_observed_segment'),
                           (1005*N + 50000001, 'future_localization_time')]:
            result = tracker.context(stamp(ns))
            self.assertFalse(result['mapping_valid'])
            self.assertIsNone(result['event_monotonic_ns'])
            self.assertEqual(result['reason'], reason)
        self.assertTrue(tracker.context(stamp(1000*N - 50000000))['mapping_valid'])
        self.assertTrue(tracker.context(stamp(1005*N + 50000000))['mapping_valid'])

    def test_ros_and_wall_jump_reset_segment_even_when_other_clock_continues(self):
        for ros, wall, cause in [(2000,1006,'ros_clock_jump'), (1006,2000,'system_clock_jump')]:
            tracker = self.tracker()
            old_segment = tracker.context(stamp(1005*N))['segment_id']
            tracker.observe(16*N,wall*N,ros*N)
            result = tracker.context(stamp(1002*N))
            self.assertNotEqual(old_segment,result['segment_id'])
            self.assertEqual(result['segment_cause'],cause)
            self.assertFalse(result['mapping_valid'])
            self.assertTrue(tracker.context(stamp(ros*N))['mapping_valid'])

    def test_rollback_overlapping_source_times_are_ambiguous(self):
        tracker = self.tracker()
        tracker.observe(16*N, 999*N, 999*N)
        tracker.observe(19*N,1002*N,1002*N)
        result = tracker.context(stamp(1001*N))
        self.assertEqual(result['reason'],'ambiguous_previous_segment')
        self.assertFalse(result['mapping_valid'])
        tracker.observe(24*N,1007*N,1007*N)
        self.assertTrue(tracker.context(stamp(1006*N))['mapping_valid'])

    def test_invalid_stamps_simulation_unknown_boot_and_slow_samples(self):
        for value in (None, {}, dict(secs=0,nsecs=0), dict(secs=1,nsecs=N), dict(secs=True,nsecs=0)):
            self.assertIsNone(stamp_nanoseconds(value))
            self.assertFalse(self.tracker().context(value)['mapping_valid'])
        tracker=SourceClockTracker('')
        tracker.observe(10*N,1000*N,1000*N)
        self.assertEqual(tracker.context(stamp(1000*N))['reason'],'unknown_boot')
        tracker=SourceClockTracker(BOOT)
        for args, expected in [((10*N,1000*N,1000*N,True),'simulation_time'),
                               ((10*N,1000*N,0),'invalid_source_clock'),
                               ((10*N,1000*N,1000*N,False,50000001),'slow_clock_sample')]:
            tracker.observe(*args)
            self.assertEqual(tracker.context(stamp(1000*N))['reason'],expected)

    def test_stored_context_is_immutable_after_later_clock_jump(self):
        tracker=self.tracker()
        result=tracker.context(stamp(1002*N))
        serialized=json.dumps(result,sort_keys=True)
        tracker.observe(16*N,2000*N,2000*N)
        self.assertEqual(json.dumps(result,sort_keys=True),serialized)


class SenderClockTests(unittest.TestCase):
    setUp = base.SenderTest.setUp
    process_next = base.SenderTest.process_next

    def test_result_and_image_reuse_same_evidence_and_original_localization_time(self):
        self.sender.input_mode='completed_target_array'
        self.sender._camera_cache_callback(base.frame())
        evidence=dict(schema_version=1,boot_id=BOOT,mapping_valid=True,event_monotonic_ns=123,
                      sample_monotonic_ns=456,segment_id='test',reason='ok')
        value=target()
        with patch.object(self.sender,'_observe_localization_clock',return_value=evidence) as observer:
            self.sender._detection_callback(NS(targets=[value]))
            raw=self.sender.result_jobs.get_nowait()
            observer.assert_called_once_with(dict(secs=value.localization_time.secs,nsecs=value.localization_time.nsecs))
            _,image_meta=self.process_next()
        self.assertEqual(raw['localization_clock'],evidence)
        self.assertEqual(image_meta['localization_clock'],evidence)
        self.assertEqual(raw['localization_time'],dict(secs=value.localization_time.secs,nsecs=value.localization_time.nsecs))

    def test_clock_sampling_and_context_are_atomic_between_callbacks(self):
        tracker=SourceClockTracker(BOOT)
        self.sender._source_clock_tracker=tracker
        first_reading=threading.Event()
        second_entered=threading.Event()
        release_first=threading.Event()
        second_clock_read=threading.Event()
        ticks=[10*N]
        values={}
        def monotonic():
            if threading.current_thread().name == 'clock-second':
                second_clock_read.set()
            value=ticks[0]
            ticks[0]+=1000
            return value
        def get_param(*args):
            if threading.current_thread().name == 'clock-second':
                second_entered.set()
            return False
        def ros_now():
            if threading.current_thread().name == 'clock-first':
                first_reading.set()
                release_first.wait(2)
            return NS(**stamp(1000*N+ticks[0]-10*N))
        def collect(name):
            values[name]=self.sender._observe_localization_clock(stamp(1000*N))
        with patch.object(base.sender_module.rospy,'get_param',side_effect=get_param), \
             patch.object(base.sender_module.rospy,'Time',NS(now=ros_now),create=True), \
             patch.object(base.sender_module.time,'monotonic_ns',side_effect=monotonic), \
             patch.object(base.sender_module.time,'time_ns',side_effect=lambda:1000*N+ticks[0]-10*N):
            first=threading.Thread(target=collect,args=('first',),name='clock-first')
            second=threading.Thread(target=collect,args=('second',),name='clock-second')
            first.start()
            self.assertTrue(first_reading.wait(1))
            second.start()
            try:
                self.assertTrue(second_entered.wait(1))
                self.assertFalse(second_clock_read.wait(.1), '另一回调不应在第一样本未记录时读取时钟')
            finally:
                release_first.set()
                first.join(2);second.join(2)
        self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
        self.assertTrue(values['first']['mapping_valid'])
        self.assertTrue(values['second']['mapping_valid'])
        self.assertEqual(values['first']['segment_id'],values['second']['segment_id'])
        self.assertLess(values['first']['sample_monotonic_ns'],values['second']['sample_monotonic_ns'])

    def test_unmappable_old_event_is_saved_and_retransmitted_without_recalculation(self):
        self.sender.input_mode='completed_target_array'
        tracker=SourceClockTracker(BOOT)
        event_stamp=target().localization_time
        event_ns=event_stamp.secs*N+event_stamp.nsecs
        tracker.observe(10*N,event_ns+10*N,event_ns+10*N)
        evidence=tracker.context(dict(secs=event_stamp.secs,nsecs=event_stamp.nsecs))
        self.assertEqual(evidence['reason'],'before_observed_segment')
        with patch.object(self.sender,'_observe_localization_clock',return_value=evidence):
            self.sender._detection_callback(NS(targets=[target()]))
        raw=self.sender.result_jobs.get_nowait()
        path=self.sender._mission_cache_dir(raw['mission_id']) / Path(raw['file_name']).with_suffix('.json')
        saved=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['localization_clock'],evidence)
        self.assertEqual(saved['localization_time'],dict(secs=event_stamp.secs,nsecs=event_stamp.nsecs))
        tracker.observe(20*N,5000*N,5000*N)
        with patch.object(self.sender,'_observe_localization_clock') as observer, \
             patch.object(self.sender,'_send_result_with_ack') as send:
            self.sender._retry_pending_results(raw['mission_id'])
        observer.assert_not_called()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]['localization_clock'],evidence)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['localization_clock'],evidence)

    def test_ros_clock_read_failure_does_not_lose_json(self):
        # 该桩没有 rospy.Time，真实采样入口返回明确的无效证据，仍缓存并传送JSON。
        self.sender.input_mode='completed_target_array'
        self.sender._detection_callback(NS(targets=[target()]))
        self.assertEqual(self.sender.result_jobs.qsize(),1)
        raw=self.sender.result_jobs.get_nowait()
        self.assertFalse(raw['localization_clock']['mapping_valid'])
        self.assertEqual(raw['localization_clock']['reason'],'clock_observation_failed')
        self.assertIn('localization_time',raw)
        json.dumps(raw,allow_nan=False)


if __name__ == '__main__':
    unittest.main()
