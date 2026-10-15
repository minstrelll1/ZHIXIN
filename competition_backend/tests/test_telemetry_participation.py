"""Mission selection must not freeze fleet monitoring or expand command scope."""

import tempfile
import unittest
from dataclasses import asdict
from unittest.mock import Mock

from competition_backend.adapter import RecordingAdapter
from competition_backend.api import create_app
from competition_backend.distributed_adapter import DistributedFleetAdapter
from competition_backend.models import Telemetry
from competition_backend.orchestrator import CompetitionOrchestrator
from test_orchestrator import FakeClock, make_config


class TelemetryParticipationTest(unittest.TestCase):
    def test_local_status_and_peer_export_follow_excluded_uav_reconnections(self):
        # Call the real endpoints directly, without starting sockets or workers.
        for uid in (4, 5):
            for selected in ([], [1, 2, 3, 6]):
                with self.subTest(uid=uid, selected=selected), tempfile.TemporaryDirectory() as directory:
                    app = create_app({
                        "COMPETITION_ADAPTER": "distributed",
                        "COMPETITION_DATA_DIR": directory,
                        "COMPETITION_LOCAL_UAV_ID": str(uid),
                        "COMPETITION_GROUND_PEERS": "",
                        "COMPETITION_PEER_TOKEN": "test-token",
                    }, audit=Mock())
                    backend = app.state.orchestrator
                    adapter = app.state.adapter
                    adapter.local_adapter.forward_command = Mock()
                    adapter._request_json = Mock(side_effect=AssertionError("unexpected network request"))
                    endpoints = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
                    backend.update_telemetry(Telemetry(uav_id=uid, received_at=0, connected=False))
                    backend.set_active_uav_ids(selected)
                    adapter.accept_peer_snapshot("ground-uav1", {
                        "mission": {"mission_id": "publisher-task", "phase": "running"},
                        "active_uav_ids": list(range(1, 7)),
                    })

                    for index, connected in enumerate((True, False, True)):
                        latitude = 33.86 + index * .0001
                        adapter.local_adapter._handle_message(uid, {
                            "type": "telemetry", "connected": connected,
                            "position": [index, 0, 0], "velocity": [0, 0, 0],
                            "latitude": latitude, "longitude": 113.70,
                            "gps_status": 6, "location_source": 4,
                        })
                        status = endpoints["/api/v1/status"]()
                        local = status["telemetry"][str(uid)]
                        shared = endpoints["/api/v1/peer/telemetry/{uav_id}"](uid, "test-token")["telemetry"]
                        self.assertEqual(local["connected"], connected)
                        self.assertEqual(local["latitude"], latitude)
                        for field in ("connected", "latitude", "longitude", "received_at", "position"):
                            self.assertEqual(local[field], shared[field], field)
                        self.assertEqual(backend.active_uav_ids, selected)
                        self.assertTrue(status["mission_read_only"])
                        self.assertEqual(status["mission"]["mission_id"], "publisher-task")
                    adapter.local_adapter.forward_command.assert_not_called()
                    adapter._request_json.assert_not_called()

    def test_excluded_peer_telemetry_still_updates_local_snapshot(self):
        adapter = DistributedFleetAdapter(range(1, 7), 1, "ground-uav1", {5: "http://peer5"}, port=0)
        backend = CompetitionOrchestrator(make_config(), adapter, active_uav_ids=[1])
        adapter._request_json = Mock()
        for index, connected in enumerate((True, False, True)):
            sample = Telemetry(uav_id=5, received_at=1000 + index, connected=connected, latitude=33.86 + index * .0001)
            adapter._request_json.return_value = {"telemetry": asdict(sample)}
            adapter._poll_peer_once(5, "http://peer5")
            status = backend.telemetry_snapshot()
            self.assertIn("5", status)
            self.assertEqual(status["5"]["connected"], connected)
            self.assertEqual(status["5"]["latitude"], sample.latitude)
        self.assertEqual(backend.active_uav_ids, [1])

    def test_planning_only_continues_monitoring_without_assigning_reconnected_uav(self):
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(make_config(), adapter)
        backend.set_active_uav_ids([])
        backend.plan("subject1")
        adapter.inject(Telemetry(uav_id=5, received_at=backend.clock(), connected=True, latitude=33.86))
        backend.tick()
        status = backend.snapshot()
        self.assertIn("5", status["telemetry"])
        self.assertTrue(status["telemetry"]["5"]["connected"])
        self.assertEqual(status["active_uav_ids"], [])
        self.assertEqual(status["mission"]["uavs"], {})
        self.assertFalse(backend.prepare_takeoff()["ready"])
        self.assertEqual(adapter.commands, [])

    def test_monitoring_all_uavs_does_not_expand_preflight_or_command_scope(self):
        clock = FakeClock()
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(make_config(), adapter, clock=clock, active_uav_ids=[1])
        backend.plan("subject1")
        runtime = backend._mission.uavs[1]
        adapter.inject(Telemetry(
            uav_id=1, received_at=clock(), connected=True, armed=True, odom_valid=True,
            control_state=2, battery_percentage=.9, position=[0, 0, runtime.target_altitude_m],
            task_assignment_acked=True, task_assignment_mission_id=backend._mission.mission_id,
            task_assignment_checksum=runtime.assignment_checksum,
        ))
        # An excluded UAV's failsafe must remain visible without blocking UAV1.
        adapter.inject(Telemetry(uav_id=5, received_at=clock(), connected=True, armed=True, failsafe=True))
        self.assertIn("5", backend.telemetry_snapshot())
        prepared = backend.prepare_takeoff()
        self.assertTrue(prepared["ready"], prepared)
        self.assertNotIn("5", prepared["preflight"]["failures"])
        backend.confirm_takeoff(prepared["confirmation_token"])
        backend.tick()
        self.assertEqual(runtime.phase.value, "executing")
        backend.request_return_all()
        self.assertEqual(backend.active_uav_ids, [1])
        self.assertEqual(set(backend._mission.uavs), {1})
        self.assertEqual({command["uav_id"] for command in adapter.commands}, {1})
        self.assertEqual({command["type"] for command in adapter.commands},
                         {"assign_task", "takeoff", "return_home"})

    def test_unconfigured_uav_is_not_added_to_monitoring(self):
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(make_config(), adapter, active_uav_ids=[1])
        adapter.inject(Telemetry(uav_id=99, received_at=backend.clock(), connected=True))
        self.assertEqual(backend.telemetry_snapshot(), {})


if __name__ == "__main__":
    unittest.main()
