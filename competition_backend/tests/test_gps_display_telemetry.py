import json
import unittest
from dataclasses import asdict
from unittest.mock import patch

from competition_backend.distributed_adapter import DistributedFleetAdapter

from competition_backend.orchestrator import CompetitionOrchestrator
from competition_backend.tcp_adapter import TcpFleetAdapter
from tests.test_orchestrator import FakeClock, make_config


class GpsDisplayTelemetryTest(unittest.TestCase):
    def setUp(self):
        self.adapter = TcpFleetAdapter([1])
        self.backend = CompetitionOrchestrator(make_config(), self.adapter, clock=FakeClock())

    def message(self, gps):
        return json.loads(json.dumps({
            'type': 'telemetry', 'uav_id': 1, 'connected': True,
            'position': [-0.6, 3.85, 1.9], 'velocity': [0.1, 0.2, 0.0],
            'latitude': 38.88025665283203, 'longitude': 121.52688598632812,
            'gps_position': gps,
        }))

    def test_precise_gps_survives_tcp_and_status_without_overwriting_enu(self):
        gps = {'latitude': 38.88025669783207, 'longitude': 121.52688592778161,
               'altitude': 40.4332841, 'age_seconds': 0.1, 'source': 'mavros_global'}
        self.adapter._handle_message(1, self.message(gps))
        item = self.backend.snapshot()['telemetry']['1']
        self.assertAlmostEqual(item['gps_position']['age_seconds'], gps['age_seconds'], places=2)
        self.assertEqual({k:v for k,v in item['gps_position'].items() if k!='age_seconds'},
                         {k:v for k,v in gps.items() if k!='age_seconds'})
        self.assertEqual(item['position'], [-0.6, 3.85, 1.9])
        self.assertEqual(item['latitude'], 38.88025665283203)
        self.assertEqual(item['longitude'], 121.52688598632812)

    def test_optional_bad_gps_does_not_break_vehicle_telemetry(self):
        for gps in (None, {}, {'latitude': 91, 'longitude': 121, 'age_seconds': 0},
                    {'latitude': 38, 'longitude': 181, 'age_seconds': 0},
                    {'latitude': 38, 'longitude': 121, 'age_seconds': 2.1},
                    {'latitude': 38, 'longitude': 121, 'age_seconds': -1},
                    {'latitude': float('nan'), 'longitude': 121, 'age_seconds': 0}):
            with self.subTest(gps=gps):
                self.adapter._handle_message(1, self.message(gps))
                item = self.backend.snapshot()['telemetry']['1']
                self.assertTrue(item['connected'])
                self.assertIsNone(item['gps_position'])
                self.assertEqual(item['position'], [-0.6, 3.85, 1.9])

    def test_precise_gps_does_not_persist_after_older_sender_omits_it(self):
        gps = {'latitude': 38.88, 'longitude': 121.52, 'age_seconds': 0}
        self.adapter._handle_message(1, self.message(gps))
        message = self.message(None)
        message.pop('gps_position')
        self.adapter._handle_message(1, message)
        self.assertIsNone(self.backend.snapshot()['telemetry']['1']['gps_position'])

    def test_heartbeat_and_task_status_do_not_refresh_position_age(self):
        gps = {'latitude': 38.88, 'longitude': 121.52, 'age_seconds': 0.1}
        with patch('competition_backend.tcp_adapter.time.monotonic', return_value=100) as mono:
            self.adapter._handle_message(1, self.message(gps))
            mono.return_value = 103
            for message in ({'type':'heartbeat'}, {'type':'task_status','status':{'state':'accepted'}}):
                self.adapter._handle_message(1, message)
                item = self.backend.snapshot()['telemetry']['1']
                self.assertEqual(item['gps_telemetry_age_seconds'], 3)
                self.assertAlmostEqual(item['gps_position']['age_seconds'], 3.1)

    def test_repeated_peer_export_ages_copy_without_mutating_cache(self):
        gps = {'latitude': 38.88, 'longitude': 121.52, 'age_seconds': 0.1}
        with patch('competition_backend.tcp_adapter.time.monotonic', return_value=100) as mono:
            self.adapter._handle_message(1, self.message(gps))
            for clock in (101, 101, 103):
                mono.return_value = clock
                exported = self.adapter.telemetry_snapshot(1)
                self.assertEqual(exported.gps_telemetry_age_seconds, clock-100)
                self.assertAlmostEqual(exported.gps_position['age_seconds'], clock-100+0.1)
            self.assertEqual(self.adapter._telemetry[1].gps_position['age_seconds'], 0.1)

    def test_peer_clock_skew_does_not_refresh_old_position(self):
        for peer_clock in (400, 1600):
            adapter = DistributedFleetAdapter([1, 2], local_uav_id=2,
                node_id='ground-uav2', peers={1:'http://peer'}, bind_host='127.0.0.1',
                port=0, uav_auth_token='', peer_token='')
            raw = asdict(self.adapter._get_telemetry_locked(1))
            raw.update(received_at=peer_clock, gps_telemetry_age_seconds=1.9,
                gps_position={'latitude':38.88, 'longitude':121.52, 'age_seconds':1.95})
            def response(*args, **kwargs):
                adapter._stop_event.set()
                return {'telemetry': raw}
            adapter._request_json = response
            with patch('competition_backend.distributed_adapter.time.time', return_value=1000), \
                    patch('competition_backend.distributed_adapter.time.monotonic', side_effect=[100,100.2]):
                adapter._poll_loop()
            item = adapter._peer_telemetry[1]
            self.assertEqual(item.received_at, 1000)
            self.assertAlmostEqual(item.gps_telemetry_age_seconds, 2.1)
            self.assertAlmostEqual(item.gps_position['age_seconds'], 2.15)
            self.assertEqual(raw['gps_position']['age_seconds'], 1.95)


if __name__ == '__main__':
    unittest.main()
