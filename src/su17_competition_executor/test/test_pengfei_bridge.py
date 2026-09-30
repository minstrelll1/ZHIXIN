import json
import math
import tempfile
import types
import unittest
from unittest.mock import patch

from su17_competition_executor.pengfei_bridge import PengfeiReadOnlyBridge


class FakeRos:
    def __init__(self):
        self.callbacks = {}
        self.warnings = []
        self.timers = []

    def Subscriber(self, topic, message_class, callback, queue_size):
        self.callbacks[topic] = callback
        return types.SimpleNamespace(unregister=lambda: self.callbacks.pop(topic, None))

    def Duration(self, seconds):
        return seconds

    def Timer(self, interval, callback):
        timer = types.SimpleNamespace(interval=interval, callback=callback, shutdown=lambda: None)
        self.timers.append(timer)
        return timer

    def logwarn(self, template, *args):
        self.warnings.append(template % args)

    def loginfo(self, *args):
        pass


class PengfeiBridgeTest(unittest.TestCase):
    def test_three_topics_are_read_only_and_serialize_empty_or_nan_safely(self):
        ros = FakeRos()
        message_types = types.SimpleNamespace(**{
            name: type(name, (), {}) for name in
            ('PengfeiCurrentTarget', 'PengfeiLastRecognition', 'PengfeiNodeStatus')
        })
        with patch('su17_competition_executor.pengfei_bridge.importlib.import_module', return_value=message_types):
            bridge = PengfeiReadOnlyBridge(ros, 4)
        self.assertEqual(sorted(ros.callbacks), sorted([
            '/uav4/pengfei/current_target', '/uav4/pengfei/last_recognition',
            '/uav4/pengfei/node_status',
        ]))
        self.assertFalse(ros.timers)
        empty = bridge.snapshot()
        self.assertIsNone(empty['current_target'])
        self.assertIsNone(empty['last_recognition'])
        self.assertIsNone(empty['node_status'])
        ros.callbacks['/uav4/pengfei/current_target'](types.SimpleNamespace(
            target_id='global-17', target_type='车辆1', latitude_deg=30.2,
            longitude_deg=103.8, altitude_gps_m=500.5,
            velocity_north_mps=float('nan'), velocity_east_mps=1.25,
        ))
        ros.callbacks['/uav4/pengfei/last_recognition'](types.SimpleNamespace(
            target_id='global-17', target_type='车辆1',
            latitude_deg=float('nan'), longitude_deg=float('nan'),
        ))
        ros.callbacks['/uav4/pengfei/node_status'](types.SimpleNamespace(
            scheduler_state='tracking', maneuver_state='unknown',
            gimbal_state='tracking', follower_state='inactive', scan_point_number=3,
        ))
        payload = bridge.snapshot()
        self.assertEqual(payload['current_target']['target_id'], 'global-17')
        self.assertIsNone(payload['current_target']['velocity_north_mps'])
        self.assertEqual(payload['current_target']['velocity_east_mps'], 1.25)
        self.assertIsNone(payload['last_recognition']['latitude_deg'])
        self.assertEqual(payload['node_status']['scan_point_number'], 3)
        for key in ('current_target', 'last_recognition', 'node_status'):
            self.assertLessEqual(payload['sent_at_unix'] - payload[key]['received_at_unix'], 1.0)
        json.dumps(payload, allow_nan=False)
        payload['current_target']['target_id'] = 'changed'
        self.assertEqual(bridge.snapshot()['current_target']['target_id'], 'global-17')

    def test_missing_message_package_does_not_break_competition_node_and_retries(self):
        ros = FakeRos()
        with patch('su17_competition_executor.pengfei_bridge.importlib.import_module', side_effect=ImportError('not deployed')):
            bridge = PengfeiReadOnlyBridge(ros, 1)
        self.assertEqual(ros.callbacks, {})
        self.assertEqual(len(ros.timers), 1)
        self.assertTrue(ros.warnings)
        self.assertIsNone(bridge.snapshot()['current_target'])
        message_types = types.SimpleNamespace(**{
            name: type(name, (), {}) for name in
            ('PengfeiCurrentTarget', 'PengfeiLastRecognition', 'PengfeiNodeStatus')
        })
        with patch('su17_competition_executor.pengfei_bridge.importlib.import_module', return_value=message_types):
            ros.timers[0].callback(None)
        self.assertEqual(len(ros.callbacks), 3)


if __name__ == '__main__':
    unittest.main()
