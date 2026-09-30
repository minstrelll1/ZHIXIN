import os
import tempfile
import time
import unittest
from dataclasses import asdict
from unittest.mock import Mock

from fastapi.testclient import TestClient
from competition_backend.api import create_app

from competition_backend.adapter import RecordingAdapter
from competition_backend.distributed_adapter import DistributedFleetAdapter
from competition_backend.models import Telemetry
from competition_backend.orchestrator import CompetitionOrchestrator
from competition_backend.tcp_adapter import TcpFleetAdapter
from test_orchestrator import make_config


class EgoTelemetryTest(unittest.TestCase):
    def test_tcp_link_preserves_zero_and_does_not_refresh_on_heartbeat(self):
        adapter = TcpFleetAdapter([1])
        adapter._handle_message(1, {
            "type": "telemetry", "connected": True,
            "position": [0, 0, 0], "velocity": [0, 0, 0],
            "ego_exec_state": 0, "ego_exec_state_age_seconds": 0.25,
        })
        item = adapter.telemetry_snapshot(1)
        self.assertEqual(item.ego_exec_state, 0)
        self.assertGreaterEqual(item.ego_exec_state_age_seconds, 0.25)

        adapter._ego_telemetry_received[1] = time.monotonic() - 4.0
        adapter._handle_message(1, {"type": "heartbeat"})
        self.assertGreater(adapter.telemetry_snapshot(1).ego_exec_state_age_seconds, 4.2)

        # 旧机载端或重连后没有 EGO 字段时，地面不能沿用上一架次的值。
        adapter._handle_message(1, {
            "type": "telemetry", "connected": True,
            "position": [0, 0, 0], "velocity": [0, 0, 0],
        })
        self.assertIsNone(adapter.telemetry_snapshot(1).ego_exec_state)
        self.assertIsNone(adapter.telemetry_snapshot(1).ego_exec_state_age_seconds)

    def test_simulated_status_endpoint_exposes_current_ego_state(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TestClient(create_app(dict(
                os.environ, COMPETITION_ADAPTER="sim", COMPETITION_DATA_DIR=folder,
            )))
            response = client.post("/api/v1/sim/uavs/1/telemetry", json={
                "connected": True, "position": [0, 0, 0], "velocity": [0, 0, 0],
                "ego_exec_state": 0, "ego_exec_state_age_seconds": 0.1,
            })
            self.assertEqual(response.status_code, 200, response.text)
            state = client.get("/api/v1/status").json()["telemetry"]["1"]
            self.assertEqual(state["ego_exec_state"], 0)
            self.assertEqual(state["ego_exec_state_age_seconds"], 0.1)

    def test_status_snapshot_and_peer_keep_original_age(self):
        backend = CompetitionOrchestrator(make_config(), RecordingAdapter(), clock=time.time)
        backend.update_telemetry(Telemetry(
            uav_id=1, received_at=time.time(), connected=True,
            ego_exec_state=5, ego_exec_state_age_seconds=0.4,
        ))
        item = backend.snapshot()["telemetry"]["1"]
        self.assertEqual(item["ego_exec_state"], 5)
        self.assertEqual(item["ego_exec_state_age_seconds"], 0.4)

        peer = DistributedFleetAdapter(
            range(1, 7), local_uav_id=2, node_id="ground-uav2",
            peers={1: "http://127.0.0.1:8000"}, port=0,
        )
        peer._request_json = Mock(return_value={"telemetry": asdict(Telemetry(
            uav_id=1, received_at=100.0, connected=True,
            ego_exec_state=4, ego_exec_state_age_seconds=0.75,
        ))})
        peer._poll_peer_once(1, "http://127.0.0.1:8000")
        self.assertEqual(peer._peer_telemetry[1].ego_exec_state, 4)
        self.assertGreaterEqual(peer._peer_telemetry[1].ego_exec_state_age_seconds, 0.75)


if __name__ == "__main__":
    unittest.main()
