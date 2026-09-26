import unittest
import time
from unittest.mock import Mock

from competition_backend.distributed_adapter import (
    DistributedFleetAdapter,
    parse_ground_peers,
)
from competition_backend.models import Telemetry


class DistributedFleetAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = DistributedFleetAdapter(
            range(1, 7),
            local_uav_id=2,
            node_id="ground-uav2",
            peers={1: "http://10.0.0.11:8000", 3: "http://10.0.0.13:8000"},
            bind_host="127.0.0.1",
            port=0,
            uav_auth_token="uav-token",
            peer_token="peer-token",
        )

    def test_local_command_never_uses_ground_network(self):
        self.adapter.local_adapter.forward_command = Mock()
        self.adapter._request_json = Mock()
        self.adapter.command_takeoff(2, {"mission_id": "m1"})
        self.adapter.local_adapter.forward_command.assert_called_once_with(
            2, "takeoff", {"mission_id": "m1"}
        )
        self.adapter._request_json.assert_not_called()

    def test_remote_command_goes_to_only_the_paired_ground_computer(self):
        self.adapter.set_task_publisher(True)
        self.adapter._request_json = Mock(return_value={"accepted": True})
        self.adapter.command_task(3, {"mission_id": "m1", "task": {"sector": 3}})
        url, = self.adapter._request_json.call_args.args
        self.assertEqual(url, "http://10.0.0.13:8000/api/v1/peer/command")
        payload = self.adapter._request_json.call_args.kwargs["payload"]
        self.assertEqual(payload["uav_id"], 3)
        self.assertEqual(payload["coordinator_id"], "ground-uav2")

    def test_peer_cannot_forward_to_a_nonlocal_uav(self):
        with self.assertRaisesRegex(ValueError, "only control UAV2"):
            self.adapter.accept_peer_command(
                "ground-uav1", 1, "takeoff", {"mission_id": "m1"}
            )

    def test_peer_parser(self):
        self.assertEqual(
            parse_ground_peers("1=http://10.0.0.11:8000;6=http://10.0.0.16:8000/"),
            {1: "http://10.0.0.11:8000", 6: "http://10.0.0.16:8000"},
        )

    def test_second_coordinator_is_rejected_while_lease_is_fresh(self):
        self.adapter.claim_peer_coordinator("ground-uav1")
        with self.assertRaisesRegex(RuntimeError, "owned by ground-uav1"):
            self.adapter.claim_peer_coordinator("ground-uav6")

    def test_follower_keeps_a_copy_of_the_coordinator_mission(self):
        snapshot = {"mission": {"mission_id": "m1", "phase": "running"}}
        self.adapter.accept_peer_snapshot("ground-uav1", snapshot)
        snapshot["mission"]["phase"] = "changed-outside"
        self.assertEqual(
            self.adapter.mirrored_snapshot()["mission"]["phase"], "running"
        )

    def test_connected_snapshot_uses_cached_peer_telemetry_without_network_probe(self):
        self.adapter._peer_telemetry[1] = Telemetry(
            uav_id=1, connected=True, received_at=time.time()
        )
        self.adapter._request_json = Mock(side_effect=AssertionError("must not probe"))
        self.assertEqual(self.adapter.connected_uav_ids_snapshot(), [1])

    def test_remote_only_coordinator_does_not_require_its_paired_uav(self):
        self.adapter.set_task_publisher(True)
        self.adapter._request_json = Mock(return_value={"accepted": True})
        self.adapter.acquire_coordination([3])
        self.assertTrue(self.adapter.is_coordinator)
        self.adapter._request_json.assert_called_once_with(
            "http://10.0.0.13:8000/api/v1/peer/lease",
            method="POST",
            payload={"coordinator_id": "ground-uav2", "action": "claim"},
        )

    def test_follower_cannot_control_or_reserve_remote_uav(self):
        self.adapter._request_json = Mock()
        for operation in (lambda: self.adapter.command_takeoff(3, {}),
                          lambda: self.adapter.acquire_coordination([3])):
            with self.assertRaises(RuntimeError):
                operation()
        self.adapter._request_json.assert_not_called()

    def test_fresh_publisher_lease_blocks_local_assignment_but_allows_return(self):
        self.adapter.local_adapter.forward_command = Mock()
        self.adapter.claim_peer_coordinator("ground-uav1")
        with self.assertRaises(RuntimeError):
            self.adapter.command_assign_task(2, {})
        self.adapter.command_return(2, {"mission_id": "m"})
        self.adapter.local_adapter.forward_command.assert_called_once_with(2, "return_home", {"mission_id": "m"})


if __name__ == "__main__":
    unittest.main()
