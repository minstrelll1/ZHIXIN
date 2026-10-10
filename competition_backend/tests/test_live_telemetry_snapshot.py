import threading
import unittest
from unittest.mock import Mock, patch

from competition_backend.adapter import RecordingAdapter
from competition_backend.models import Telemetry
from competition_backend.orchestrator import CompetitionOrchestrator, MissionError
from test_orchestrator import FakeClock, make_config


class LiveTelemetrySnapshotTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.adapter = RecordingAdapter()
        self.backend = CompetitionOrchestrator(make_config(), self.adapter,
                                              live_mode=True, clock=self.clock,
                                              active_uav_ids=[1, 2, 4, 5])
        self.backend.plan('subject1')
        self.refresh()

    def refresh(self):
        mission = self.backend._mission
        for uid in [1, 2, 4, 5]:
            self.backend.update_telemetry(Telemetry(
                uav_id=uid, received_at=self.clock(), connected=True, armed=True,
                odom_valid=True, failsafe=False, control_state=2, battery_percentage=.9,
                position=[0, 0, 0], velocity=[0, 0, 0],
                gps_status=6, location_source=4, latitude=34.1, longitude=113.9,
                gps_position={'latitude':34.1, 'longitude':113.9, 'age_seconds':.05},
                task_assignment_acked=True, task_assignment_mission_id=mission.mission_id,
                task_assignment_checksum=mission.uavs[uid].assignment_checksum))

    def test_mission_lock_cannot_block_telemetry_ingest_or_gps_read(self):
        done = threading.Event()
        result = {}
        def receive():
            self.refresh()
            result.update(self.backend.telemetry_snapshot())
            done.set()
        with self.backend._lock:
            worker = threading.Thread(target=receive)
            worker.start()
            completed_while_mission_busy = done.wait(1.0)
        worker.join(2.0)
        self.assertTrue(completed_while_mission_busy)
        self.assertEqual(set(result), {'1', '2', '4', '5'})

    def test_telemetry_snapshot_does_not_serialize_routes_and_is_detached(self):
        with patch.object(self.backend._mission, 'to_dict', side_effect=AssertionError('route copy')):
            result = self.backend.telemetry_snapshot()
        result['1']['gps_position']['latitude'] = 0
        self.assertEqual(self.backend.telemetry_snapshot()['1']['gps_position']['latitude'], 34.1)

    def test_full_snapshot_samples_telemetry_after_slow_route_copy(self):
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        result = {}
        original = self.backend._mission.to_dict
        def slow_routes():
            started.set()
            if not release.wait(2):
                raise AssertionError('test did not release route serialization')
            return original()
        def collect():
            result.update(self.backend.snapshot())
            done.set()
        with patch.object(self.backend._mission, 'to_dict', side_effect=slow_routes):
            worker = threading.Thread(target=collect)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.clock.advance(1)
                self.refresh()
            finally:
                release.set()
            self.assertTrue(done.wait(2))
            worker.join(2)
        self.assertEqual(result['telemetry']['1']['received_at'], self.clock())
        self.assertEqual(result['dispatch_status']['acknowledged_uav_ids'], [1, 2, 4, 5])

    def test_tick_cannot_mix_connected_and_armed_from_different_samples(self):
        from competition_backend.models import MissionPhase, UavPhase

        adapter = RecordingAdapter()
        adapter.release_coordination = Mock()
        backend = CompetitionOrchestrator(make_config(), adapter, clock=self.clock,
                                          active_uav_ids=[1])
        backend.plan('subject1')
        backend._mission.phase = MissionPhase.TAKING_OFF
        backend._mission.uavs[1].phase = UavPhase.ERROR
        read_connected, published = threading.Event(), threading.Event()

        class ConcurrentSample(Telemetry):
            # Schedule a new sample exactly between the final connected/armed
            # reads. Neither real sample says "connected and disarmed".
            def __getattribute__(self, name):
                value = super().__getattribute__(name)
                if name == 'connected':
                    state = super().__getattribute__('__dict__')
                    state['connected_reads'] = state.get('connected_reads', 0) + 1
                    if state['connected_reads'] == 2:
                        read_connected.set()
                        if not published.wait(1.0):
                            raise AssertionError('telemetry update blocked by mission lock')
                return value

        old = ConcurrentSample(uav_id=1, received_at=self.clock(), connected=True,
                               armed=True, battery_percentage=.9)
        new = Telemetry(uav_id=1, received_at=self.clock(), connected=False,
                        armed=False, battery_percentage=.9)
        backend._telemetry = {1: old}

        def receive():
            if read_connected.wait(1.0):
                backend.update_telemetry(new)
                published.set()

        worker = threading.Thread(target=receive)
        worker.start()
        try:
            backend.tick()
        finally:
            published.set()
            worker.join(2.0)
        self.assertTrue(read_connected.is_set())
        self.assertIs(backend._telemetry[1], new)
        self.assertEqual(backend._mission.phase, MissionPhase.TAKING_OFF)
        adapter.release_coordination.assert_not_called()

    def test_mission_call_preserves_preflight_details_and_plain_errors(self):
        from fastapi import HTTPException
        from competition_backend.api import _mission_call

        report = {'ok': False, 'failures': {'1': ['telemetry is stale']}}
        cases = (
            (MissionError('preflight changed before confirmation', preflight=report),
             {'message': 'preflight changed before confirmation', 'preflight': report}),
            (MissionError('no mission exists'), 'no mission exists'),
        )
        for error, expected_detail in cases:
            with self.subTest(message=str(error)):
                with self.assertRaises(HTTPException) as caught:
                    _mission_call(Mock(side_effect=error))
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(caught.exception.detail, expected_detail)

    def test_changed_preflight_preserves_failure_details_and_sends_no_takeoff(self):
        prepared = self.backend.prepare_takeoff()
        self.assertTrue(prepared['ready'])
        self.clock.advance(5.1)
        with self.assertRaises(MissionError) as caught:
            self.backend.confirm_takeoff(prepared['confirmation_token'])
        report = caught.exception.preflight
        for uid in ('1','2','4','5'):
            self.assertIn('telemetry is stale', report['failures'][uid])
            self.assertGreater(report['telemetry_age_seconds'][uid], 5)
        self.assertIsNone(self.backend._confirmation_token)
        self.assertEqual(self.backend._mission.phase.value, 'planned')
        self.assertIsNone(self.backend._mission.confirmation_expires_at)
        self.assertEqual(self.backend._mission.events[-1]['preflight'], report)
        self.assertFalse(any(c['type']=='takeoff' for c in self.adapter.commands))
        self.refresh()
        retry = self.backend.prepare_takeoff()
        self.assertTrue(retry['ready'])
        self.assertNotEqual(prepared['confirmation_token'], retry['confirmation_token'])

    def test_preflight_accepts_up_to_five_seconds_but_keeps_disconnection_guard(self):
        for uid, item in self.backend._telemetry.items():
            item.position[2]=self.backend._mission.uavs[uid].target_altitude_m
        self.clock.advance(5.0)
        prepared=self.backend.prepare_takeoff()
        self.assertTrue(prepared['ready'])
        self.assertEqual(prepared['preflight']['preflight_telemetry_max_age_seconds'],5.)
        self.backend.confirm_takeoff(prepared['confirmation_token'])
        self.assertEqual([c['uav_id'] for c in self.adapter.commands if c['type']=='takeoff'],[1,2,4,5])
        # 飞行监控的门槛仍为2秒；不能将此旧样本用于确认到达高度。
        self.assertEqual(self.backend.config.safety.telemetry_max_age_seconds,2.)
        self.backend.tick()
        self.assertTrue(all(r.phase.value=='takeoff_commanded' for r in self.backend._mission.uavs.values()))
        self.refresh()
        self.backend._telemetry[5].connected=False
        report=self.backend.preflight_report()
        self.assertFalse(report['ok'])
        self.assertIn('flight controller is disconnected',report['failures']['5'])

    def test_confirm_after_deadline_rechecks_against_five_seconds(self):
        self.clock.advance(4.9)
        prepared=self.backend.prepare_takeoff()
        self.assertTrue(prepared['ready'])
        self.clock.advance(.2)
        with self.assertRaises(MissionError):
            self.backend.confirm_takeoff(prepared['confirmation_token'])
        self.assertFalse(any(c['type']=='takeoff' for c in self.adapter.commands))

    def test_all_scenes_use_same_five_second_preflight_window(self):
        self.clock.advance(4.0)
        for scene in ('lab','outdoor5','lab10','outdoor100','outdoor200','competition','dalian_nanshan','xuchang_small'):
            with self.subTest(scene=scene):
                self.backend._mission.flight_profile=scene
                report=self.backend.preflight_report()
                self.assertTrue(report['ok'],report)
                self.assertEqual(report['preflight_telemetry_max_age_seconds'],5.)

    def test_legacy_config_defaults_to_five_without_changing_flight_limit(self):
        import json, tempfile
        from pathlib import Path
        from competition_backend.config import load_config
        raw=json.loads((Path(__file__).resolve().parents[1]/'config/competition.example.json').read_text(encoding='utf-8'))
        raw['safety'].pop('preflight_telemetry_max_age_seconds')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text(json.dumps(raw),encoding='utf-8')
            config=load_config(str(path))
            self.assertEqual(config.safety.preflight_telemetry_max_age_seconds,5.)
            self.assertEqual(config.safety.telemetry_max_age_seconds,2.)
            for value in (0,-1,float('inf'),float('nan')):
                with self.subTest(value=value):
                    raw['safety']['preflight_telemetry_max_age_seconds']=value
                    path.write_text(json.dumps(raw),encoding='utf-8')
                    with self.assertRaises(ValueError): load_config(str(path))

    def test_cancelled_confirmation_can_be_replaced_only_by_new_manual_preflight(self):
        first = self.backend.prepare_takeoff()
        second = self.backend.prepare_takeoff()
        self.assertNotEqual(first['confirmation_token'], second['confirmation_token'])
        self.assertFalse(any(c['type']=='takeoff' for c in self.adapter.commands))
        self.backend.confirm_takeoff(second['confirmation_token'])
        self.assertEqual([c['uav_id'] for c in self.adapter.commands if c['type']=='takeoff'], [1,2,4,5])

    def test_failsafe_still_invalidates_new_preflight(self):
        first = self.backend.prepare_takeoff()
        self.backend._telemetry[4].failsafe = True
        retry = self.backend.prepare_takeoff()
        self.assertFalse(retry['ready'])
        self.assertIn('failsafe is active', retry['preflight']['failures']['4'])
        self.assertIsNone(self.backend._confirmation_token)
        with self.assertRaises(MissionError):
            self.backend.confirm_takeoff(first['confirmation_token'])
        self.assertFalse(any(c['type']=='takeoff' for c in self.adapter.commands))


if __name__ == '__main__':
    unittest.main()
