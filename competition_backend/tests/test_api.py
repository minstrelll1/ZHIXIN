import base64
import os
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from competition_backend.api import create_app


class ApiSmokeTest(unittest.TestCase):
    def setUp(self):
        self.data_directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(
            os.environ,
            {"COMPETITION_DATA_DIR": self.data_directory.name},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.data_directory.cleanup()

    def test_distributed_plan_without_any_uav_is_planning_only(self):
        environment = {
            "COMPETITION_ADAPTER": "distributed",
            "COMPETITION_LOCAL_UAV_ID": "1",
            "COMPETITION_GROUND_PEERS": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            app = create_app()
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/v1/plan")
        result = endpoint({"subject": "subject1"})
        self.assertEqual(result["active_uav_ids"], [])
        self.assertEqual(result["dispatch_status"]["mode"], "planning_only")
        self.assertEqual(set(result["mission"]["planned_uavs"]), {str(value) for value in range(1, 7)})
        self.assertEqual(result["mission"]["uavs"], {})
        self.assertFalse(app.state.adapter.is_coordinator)

    def test_groundstation_shared_http_ingest(self):
        raw = struct.pack("<fff", 1.0, 2.0, 3.0)
        payload = {
            "op": "publish",
            "topic": "/uav1/octomap_point_cloud_centers/reduce_the_frequency",
            "msg": {
                "header": {"frame_id": "map", "stamp": {"secs": 1, "nsecs": 0}},
                "height": 1, "width": 1, "point_step": 12,
                "fields": [
                    {"name": "x", "offset": 0, "datatype": 7, "count": 1},
                    {"name": "y", "offset": 4, "datatype": 7, "count": 1},
                    {"name": "z", "offset": 8, "datatype": 7, "count": 1},
                ],
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
        environment = {
            "COMPETITION_POINTCLOUD_SOURCE": "groundstation_shared",
            "COMPETITION_POINTCLOUD_RELAY_UAV_IDS": "1",
            "COMPETITION_POINTCLOUD_INGEST_TOKEN": "shared-test-token",
            "COMPETITION_POINTCLOUD_ROOT": self.data_directory.name,
        }
        with patch.dict(os.environ, environment, clear=False):
            app = create_app()
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/pointcloud/1/ingest", json=payload,
                    headers={"X-Pointcloud-Token": "shared-test-token"},
                )
                self.assertEqual(response.status_code, 200)
                latest = client.get("/api/v1/pointcloud/1/latest")
                self.assertEqual(latest.status_code, 200)
                self.assertEqual(latest.json()["points"], [[1.0, 2.0, 3.0]])

    def test_plan_preflight_and_confirm_takeoff(self):
        app = create_app()
        with TestClient(app) as client:
            frontend = client.get("/")
            self.assertEqual(frontend.status_code, 200)
            self.assertIn("六机竞赛任务指挥台", frontend.text)
            self.assertNotIn("Plan Mission", frontend.text)
            video_sources = client.get("/api/v1/video-sources")
            self.assertEqual(video_sources.status_code, 200)
            self.assertEqual(video_sources.json(), {"sources": {}})

            response = client.post("/api/v1/plan", json={"subject": "subject1"})
            self.assertEqual(response.status_code, 200)

            for uav_id in range(1, 7):
                response = client.post(
                    "/api/v1/sim/uavs/%d/telemetry" % uav_id,
                    json={
                        "connected": True,
                        "armed": True,
                        "odom_valid": True,
                        "control_state": 2,
                        "battery_percentage": 0.9,
                        "position": [0, 0, 0],
                        "velocity": [0, 0, 0],
                    },
                )
                self.assertEqual(response.status_code, 200)

            prepared = client.post("/api/v1/takeoff/prepare")
            self.assertEqual(prepared.status_code, 200)
            token = prepared.json()["confirmation_token"]
            confirmed = client.post(
                "/api/v1/takeoff/confirm", json={"token": token}
            )
            self.assertEqual(confirmed.status_code, 200)

            commands = client.get("/api/v1/sim/commands").json()["commands"]
            self.assertEqual(
                len([item for item in commands if item["type"] == "takeoff"]), 6
            )
            with client.websocket_connect("/ws/status") as websocket:
                status = websocket.receive_json()
                self.assertEqual(status["mission"]["phase"], "taking_off")


if __name__ == "__main__":
    unittest.main()
