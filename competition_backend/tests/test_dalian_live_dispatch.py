"""大连固定 WGS84 方案在真实分布式终端中的规划与模式守护。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_shared.fleet import apply_fixed_binding, default_fleet


class DalianLiveDispatchTest(unittest.TestCase):
    def _terminal(self, directory, terminal_id, publisher):
        fleet = default_fleet()
        for uid in range(1, 7):
            fleet = apply_fixed_binding(fleet, uid, "p600")
        config = Path(directory) / "fleet.json"
        config.write_text(json.dumps(fleet), encoding="utf-8")
        app = create_app(dict(
            os.environ, COMPETITION_ADAPTER="distributed",
            COMPETITION_FLEET_CONFIG=str(config), COMPETITION_DATA_DIR=directory,
            COMPETITION_GROUND_TERMINAL_ID=str(terminal_id),
            COMPETITION_ASYNC_OPERATOR_SELECTION="0",
            COMPETITION_TASK_PUBLISHER="", COMPETITION_PEER_TOKEN="test-peer",
        ))
        adapter = app.state.adapter
        adapter.peer_operator_status = Mock(return_value={})
        adapter.synchronize_fleet_config = Mock(return_value={"complete": True, "peers": {}})
        adapter.acquire_coordination = Mock()
        adapter.assert_local_control_available = Mock()
        adapter.local_adapter.forward_command = Mock()
        adapter.connected_uav_ids_snapshot = Mock(return_value=[terminal_id])
        client = TestClient(app)
        selected = client.post("/api/v1/operator", json={
            "ground_terminal_id": terminal_id, "model": "p600", "task_publisher": publisher,
        })
        self.assertEqual(selected.status_code, 200, selected.text)
        return app, client

    def test_publisher_uses_fixed_wgs84_even_with_connected_drone_without_valid_gps(self):
        with tempfile.TemporaryDirectory() as data:
            app, client = self._terminal(data, 1, True)
            app.state.orchestrator.update_telemetry(Telemetry(
                uav_id=1, received_at=app.state.orchestrator.clock(),
                connected=True, latitude=0, longitude=0, gps_status=0, location_source=0,
            ))
            app.state.program_manager = Mock()
            app.state.program_manager.snapshot.return_value = {
                "programs": {"flight": {"state": "running", "flight_mode": "outdoor",
                                        "reconnaissance_radius_m": 30.0}}
            }
            payload = {"subject": "subject1", "planning_mode": "competition",
                       "flight_profile": "dalian_nanshan", "coordinate_mode": "gps",
                       "departure_point": "fixed_dalian", "controller_mode": "external",
                       "flight_altitude_plan": "around10m",
                       "gps_origin": {"latitude": 1.0, "longitude": 2.0}}
            preview = client.post("/api/v1/planning/competition-coverage", json=payload)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["search_area"]["gps_origin"]["latitude"], 39.052820490783674)
            self.assertEqual(preview.json()["search_area"]["gps_origin"]["longitude"], 121.65960216424625)
            planned = client.post("/api/v1/plan", json=payload)
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertEqual(planned.json()["dispatch_status"]["assigned_uav_ids"], [1])
            self.assertIn("30.0m", planned.json()["planning_warnings"][0])
            command = app.state.adapter.local_adapter.forward_command.call_args.args
            self.assertEqual(command[:2], (1, "assign_task"))
            self.assertEqual(command[2]["coordinate_frame"], "WGS84")
            self.assertEqual(command[2]["target_altitude_m"], 8.0)
            self.assertEqual(command[2]["task"]["waypoints_wgs84"],
                             preview.json()["planned_uavs"]["1"]["task"]["waypoints_wgs84"])
            self.assertNotEqual(command[2]["task"]["waypoints_wgs84"][0][0], 1.0)

    def test_nonpublisher_accepts_any_program_b_flight_mode(self):
        with tempfile.TemporaryDirectory() as data:
            app, client = self._terminal(data, 2, False)
            manager = Mock()
            app.state.program_manager = manager
            payload = {"subject": "subject2", "planning_mode": "competition",
                       "flight_profile": "dalian_nanshan", "coordinate_mode": "gps",
                       "departure_point": "fixed_dalian", "controller_mode": "external"}
            bad_mode = client.post("/api/v1/plan", json={**payload, "planning_mode": "manual"})
            self.assertEqual(bad_mode.status_code, 422, bad_mode.text)
            manager.snapshot.return_value = {"programs": {"flight": {
                "state": "running", "flight_mode": "outdoor_small_range"}}}
            mismatch = client.post("/api/v1/plan", json=payload)
            self.assertEqual(mismatch.status_code, 200, mismatch.text)
            self.assertEqual(mismatch.json()["dispatch_status"]["assigned_uav_ids"], [2])
            manager.snapshot.return_value = {"programs": {"flight": {
                "state": "running", "flight_mode": "unknown"}}}
            unknown = client.post("/api/v1/plan", json=payload)
            self.assertEqual(unknown.status_code, 200, unknown.text)
            # B 未启动也不阻断起飞前规划；一键起飞仍单独检查其运行状态。
            manager.snapshot.return_value = {"programs": {"flight": {
                "state": "error", "flight_mode": "unknown"}}}
            planned = client.post("/api/v1/plan", json=payload)
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertEqual(planned.json()["dispatch_status"]["assigned_uav_ids"], [2])
            command = app.state.adapter.local_adapter.forward_command.call_args.args
            self.assertEqual(command[:2], (2, "assign_task"))
            self.assertEqual(command[2]["task"]["waypoints_wgs84"][0],
                             planned.json()["mission"]["planned_uavs"]["2"]["task"]["waypoints_wgs84"][0])
            self.assertGreater(command[2]["task"]["waypoints_wgs84"][0][1], 121.65)

    def test_outdoor5_allows_program_b_outdoor_mode(self):
        with tempfile.TemporaryDirectory() as data:
            app, client = self._terminal(data, 1, True)
            app.state.program_manager = Mock()
            app.state.program_manager.snapshot.return_value = {"programs": {"flight": {
                "state": "running", "flight_mode": "outdoor"}}}
            planned = client.post("/api/v1/plan", json={
                "subject": "subject2", "planning_mode": "competition",
                "flight_profile": "outdoor5", "coordinate_mode": "xyz",
                "controller_mode": "external",
            })
            self.assertEqual(planned.status_code, 200, planned.text)
            self.assertEqual(planned.json()["dispatch_status"]["assigned_uav_ids"], [1])

    def test_outdoor100_external_assignment_omits_planning_speed(self):
        with tempfile.TemporaryDirectory() as data:
            app, client = self._terminal(data, 1, True)
            planned = client.post("/api/v1/plan", json={
                "subject": "subject2", "planning_mode": "competition",
                "flight_profile": "outdoor100", "coordinate_mode": "xyz",
                "controller_mode": "external",
            })
            self.assertEqual(planned.status_code, 200, planned.text)
            task = app.state.adapter.local_adapter.forward_command.call_args.args[2]["task"]
            self.assertNotIn("speed_mps", task)
            self.assertEqual(planned.json()["mission"]["planned_uavs"]["1"]["task"]["speed_mps"], 5.0)


if __name__ == "__main__":
    unittest.main()
