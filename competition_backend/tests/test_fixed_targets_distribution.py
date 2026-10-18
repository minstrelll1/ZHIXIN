import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from competition_backend.api import create_app
from competition_backend.assignment_protocol import assignment_checksum
from competition_backend.models import Telemetry
from competition_shared.fleet import apply_fixed_binding, default_fleet
from competition_shared.fixed_targets import empty_fixed_targets
from test_fixed_targets import targets


class FixedTargetDistributionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        fleet = default_fleet()
        for uid in range(1, 7):
            fleet = apply_fixed_binding(fleet, uid, "p600")
            fleet["vehicles"][uid - 1]["peer_host"] = "192.0.2.%d" % (100 + uid)
        self.apps, self.routes = {}, {}
        for uid in range(1, 7):
            root = Path(self.directory.name) / str(uid)
            root.mkdir()
            config = root / "fleet.json"
            config.write_text(json.dumps(fleet), encoding="utf-8")
            app = create_app({
                "COMPETITION_ADAPTER": "distributed", "COMPETITION_FLEET_CONFIG": str(config),
                "COMPETITION_GROUND_TERMINAL_ID": str(uid), "COMPETITION_DATA_DIR": str(root / "data"),
                "COMPETITION_IMAGE_ROOT": str(root / "images"), "COMPETITION_PEER_TOKEN": "test-token",
            }, audit=Mock())
            self.apps[uid] = app
            self.routes[uid] = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
            app.state.adapter._request_json = self.network
            app.state.adapter.local_adapter.forward_command = Mock()
        for uid in (2, 3, 4, 5, 6, 1):
            self.routes[uid]["/api/v1/operator"]({"ground_terminal_id": uid, "model": "p600", "task_publisher": uid == 1})

    def network(self, url, method="GET", payload=None, **kwargs):
        for uid in self.apps:
            prefix = "http://192.0.2.%d:8000" % (100 + uid)
            if url.startswith(prefix + "/"):
                path = url[len(prefix):]
                if path.startswith("/api/v1/peer/telemetry/"):
                    return self.routes[uid]["/api/v1/peer/telemetry/{uav_id}"](uid, "test-token")
                endpoint = self.routes[uid][path]
                return endpoint(payload, "test-token") if method == "POST" else endpoint("test-token")
        raise AssertionError("Unexpected network address: " + url)

    def poll(self, uid):
        self.apps[uid].state.adapter._poll_peer_once(1, "http://192.0.2.101:8000")

    def test_initial_fleet_sync_and_reconnection_take_publisher_copy_without_aircraft(self):
        source = self.apps[1].state.fixed_target_store
        snapshot = targets()
        snapshot["targets"].append(dict(target_id="target-002", target_category="固定",
                                        target_type="车辆", target_model="车辆5", category_id=4,
                                        latitude=33.864, longitude=113.7094))
        source.replace(snapshot)
        self.apps[1].state.adapter.synchronize_fleet_config(self.apps[1].state.fleet_store.read())
        for uid in range(2, 7):
            self.assertEqual(self.apps[uid].state.fixed_target_store.read(), source.read())
        follower = self.apps[5].state.adapter
        follower._request_json = Mock(side_effect=TimeoutError("offline"))
        # Simulate a post-flight manual edit while the ground link is down.
        source.path.write_text(json.dumps(targets("subject1-new", 113.72)), encoding="utf-8")
        self.poll(5)
        self.assertEqual(self.apps[5].state.fixed_target_store.read(), snapshot)
        follower._request_json = self.network
        self.poll(5)
        self.assertEqual(self.apps[5].state.fixed_target_store.read(), source.read())
        source.replace(empty_fixed_targets())
        self.poll(5)
        self.assertEqual(self.apps[5].state.fixed_target_store.read()["targets"], [])
        for app in self.apps.values():
            app.state.adapter.local_adapter.forward_command.assert_not_called()

    def test_nonpublisher_cannot_replace_authority_and_broken_file_does_not_break_telemetry(self):
        source = self.apps[1].state.fixed_target_store
        source.replace(targets())
        self.apps[2].state.fixed_target_store.replace(targets("subject1-other", 113.73))
        self.apps[1].state.adapter._poll_peer_once(2, "http://192.0.2.102:8000")
        self.assertEqual(source.read(), targets())
        source.path.write_text("broken-json", encoding="utf-8")
        result = self.routes[1]["/api/v1/peer/telemetry/{uav_id}"](1, "test-token")
        self.assertIsNone(result["fixed_targets"])
        self.assertIn("telemetry", result)

    def test_both_subjects_dispatch_same_snapshot_to_all_six_with_checksum(self):
        publisher = self.apps[1]
        snapshot = targets()
        snapshot["targets"].append(dict(target_id="target-002", target_category="固定",
                                        target_type="人员", target_model="人员1", category_id=11,
                                        latitude=33.8644, longitude=113.7098))
        publisher.state.fixed_target_store.replace(snapshot)
        publisher.state.adapter.connected_uav_ids_snapshot = Mock(return_value=list(range(1, 7)))
        for uid in range(1, 7):
            publisher.state.orchestrator.update_telemetry(Telemetry(
                uav_id=uid, received_at=time.time(), connected=True, odom_valid=True,
            ))
        for subject in ("subject1", "subject2"):
            for app in self.apps.values():
                app.state.adapter.local_adapter.forward_command.reset_mock()
            self.routes[1]["/api/v1/plan"]({"subject": subject})
            for uid, app in self.apps.items():
                calls = app.state.adapter.local_adapter.forward_command.call_args_list
                self.assertEqual(len(calls), 1)
                actual_uid, kind, payload = calls[0].args
                self.assertEqual((actual_uid, kind), (uid, "assign_task"))
                self.assertEqual(payload["fixed_targets"], snapshot)
                message = dict(payload, type=kind, uav_id=uid)
                self.assertEqual(message["assignment_checksum"], assignment_checksum(message))
            self.assertEqual(publisher.state.fixed_target_store.read(), snapshot)


if __name__ == "__main__":
    unittest.main()
