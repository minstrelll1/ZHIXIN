import time
import unittest
from dataclasses import asdict
from unittest.mock import Mock

from competition_backend.distributed_adapter import DistributedFleetAdapter
from competition_backend.models import Telemetry
from competition_backend.orchestrator import CompetitionOrchestrator
from competition_backend.pengfei_telemetry import sanitize_pengfei
from competition_backend.tcp_adapter import TcpFleetAdapter
from competition_backend.adapter import RecordingAdapter
from test_orchestrator import make_config


def pengfei_sample():
    return {
        "sent_at_unix": 100.5,
        "current_target": {
            "received_at_unix": 100.4,
            "target_id": "target-7", "target_type": "车辆1",
            "latitude_deg": 30.785, "longitude_deg": 103.861,
            "altitude_gps_m": float("nan"),
            "velocity_north_mps": 1.2, "velocity_east_mps": -0.3,
        },
        "last_recognition": {
            "received_at_unix": 100.4,
            "target_id": "target-7", "target_type": "车辆1",
            "latitude_deg": 30.784, "longitude_deg": 103.860,
        },
        "node_status": {
            "received_at_unix": 100.4,
            "scheduler_state": "tracking", "maneuver_state": "inactive",
            "gimbal_state": "tracking", "follower_state": "inactive",
            "scan_point_number": 2,
        },
    }


class PengfeiGroundTelemetryTest(unittest.TestCase):
    def test_tcp_snapshot_keeps_three_topics_and_nulls_nan(self):
        adapter = TcpFleetAdapter([1])
        received = []
        adapter.set_telemetry_sink(received.append)
        adapter._handle_message(1, {
            "type": "telemetry", "connected": True,
            "position": [0, 0, 0], "velocity": [0, 0, 0],
            "pengfei": pengfei_sample(),
        })
        item = adapter.telemetry_snapshot(1)
        self.assertEqual(item.pengfei["current_target"]["target_id"], "target-7")
        self.assertEqual(item.pengfei["last_recognition"]["latitude_deg"], 30.784)
        self.assertEqual(item.pengfei["node_status"]["scan_point_number"], 2)
        self.assertIsNone(item.pengfei["current_target"]["altitude_gps_m"])
        self.assertIsNotNone(item.pengfei_age_seconds)
        self.assertIsNone(sanitize_pengfei({**pengfei_sample(), "current_target": {
            **pengfei_sample()["current_target"], "latitude_deg": 1000,
        }})["current_target"]["latitude_deg"])

        # A heartbeat must not make an old ROS topic appear fresh.
        adapter._pengfei_telemetry_received[1] = time.monotonic() - 4
        adapter._handle_message(1, {"type": "heartbeat"})
        self.assertGreater(received[-1].pengfei_age_seconds, 3.9)

        # Reconnecting with an older onboard version clears the previous target.
        adapter._handle_message(1, {
            "type": "telemetry", "connected": True,
            "position": [0, 0, 0], "velocity": [0, 0, 0],
        })
        self.assertEqual(adapter.telemetry_snapshot(1).pengfei, {})
        self.assertIsNone(adapter.telemetry_snapshot(1).pengfei_age_seconds)

    def test_snapshot_and_peer_forwarding_preserve_data_with_local_age(self):
        sample = sanitize_pengfei(pengfei_sample())
        backend = CompetitionOrchestrator(make_config(), RecordingAdapter(), clock=time.time)
        backend.update_telemetry(Telemetry(
            uav_id=1, received_at=time.time(), connected=True,
            pengfei=sample, pengfei_age_seconds=0.25,
        ))
        snap = backend.snapshot()["telemetry"]["1"]
        self.assertEqual(snap["pengfei"]["node_status"]["follower_state"], "inactive")
        self.assertEqual(snap["pengfei_age_seconds"], 0.25)
        snap["pengfei"]["node_status"]["follower_state"] = "outside mutation"
        self.assertEqual(backend.snapshot()["telemetry"]["1"]["pengfei"]["node_status"]["follower_state"], "inactive")

        peer = DistributedFleetAdapter(
            range(1, 7), local_uav_id=2, node_id="ground-uav2",
            peers={1: "http://127.0.0.1:8000"}, port=0,
        )
        peer._request_json = Mock(return_value={"telemetry": asdict(Telemetry(
            uav_id=1, received_at=100.0, connected=True,
            pengfei=sample, pengfei_age_seconds=0.25,
        ))})
        peer._poll_peer_once(1, "http://127.0.0.1:8000")
        forwarded = peer._peer_telemetry[1]
        self.assertEqual(forwarded.pengfei["current_target"]["target_id"], "target-7")
        self.assertGreaterEqual(forwarded.pengfei_age_seconds, 0.25)
        self.assertLess(time.time() - forwarded.received_at, 1.0)


if __name__ == "__main__":
    unittest.main()
