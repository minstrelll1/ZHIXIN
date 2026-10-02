"""Regression tests for the GPS origin used by fixed-area planning."""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from unittest.mock import Mock
import time

from competition_backend.models import Telemetry

from competition_backend.api import _fresh_onboard_gps_reference, create_app


class GpsPlanningReferenceTest(unittest.TestCase):
    def telemetry(self):
        return {
            "connected": True,
            "received_at": 999.7,
            "gps_status": 3,
            "location_source": 4,
            # Prometheus UAVState stores these coordinates as float32.
            "latitude": 38.88025665283203,
            "longitude": 121.52688598632812,
            "gps_position": {
                "latitude": 38.880256421,
                "longitude": 121.526884317,
                "age_seconds": 0.2,
                "source": "mavros_global",
            },
        }

    def test_fresh_precise_fix_precedes_low_precision_uav_state(self):
        reference = _fresh_onboard_gps_reference(self.telemetry(), 1000.0, 2.0, 1)
        self.assertIsNotNone(reference)
        self.assertEqual(reference["latitude"], 38.880256421)
        self.assertEqual(reference["longitude"], 121.526884317)
        self.assertEqual(reference["source"], "mavros_global")

    def test_stale_precise_fix_falls_back_to_fresh_low_precision_uav_state(self):
        item = self.telemetry()
        # GPS age at arrival plus time since telemetry arrival exceeds 2 s.
        item["gps_position"]["age_seconds"] = 1.8
        reference = _fresh_onboard_gps_reference(item, 1000.0, 2.0, 1)
        self.assertIsNotNone(reference)
        self.assertEqual(reference["latitude"], item["latitude"])
        self.assertEqual(reference["longitude"], item["longitude"])
        self.assertEqual(reference["source"], "prometheus_gps_low_precision")

    def test_stale_or_invalid_telemetry_is_not_a_planning_origin(self):
        item = self.telemetry()
        item["received_at"] = 997.9
        self.assertIsNone(_fresh_onboard_gps_reference(item, 1000.0, 2.0, 1))

        item = self.telemetry()
        item["gps_position"] = None
        item["gps_status"] = 2
        self.assertIsNone(_fresh_onboard_gps_reference(item, 1000.0, 2.0, 1))

        item["gps_status"] = 3
        item["location_source"] = 2
        self.assertIsNone(_fresh_onboard_gps_reference(item, 1000.0, 2.0, 1))

    def test_offline_live_gps_preview_uses_page_origin_without_dispatch(self):
        with tempfile.TemporaryDirectory() as data_directory:
            environment = dict(os.environ)
            environment.update({
                "COMPETITION_ADAPTER": "distributed",
                "COMPETITION_DATA_DIR": data_directory,
                "COMPETITION_LOCAL_UAV_ID": "1",
                "COMPETITION_GROUND_PEERS": "",
            })
            app = create_app(environment)
            client = TestClient(app)
            self.assertEqual(app.state.adapter.connected_uav_ids_snapshot(), [])
            response = client.post("/api/v1/planning/competition-coverage", json={
                "subject": "subject1",
                "coordinate_mode": "gps",
                "flight_profile": "lab",
                "gps_origin": {"latitude": 30.78528, "longitude": 103.86102},
            })
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()
            self.assertTrue(preview["preview_only"])
            self.assertEqual(len(preview["planned_uavs"]), 6)
            self.assertEqual(preview["search_area"]["gps_origin"]["latitude"], 30.78528)
            self.assertEqual(preview["search_area"]["gps_origin"]["longitude"], 103.86102)
            self.assertIsNone(app.state.orchestrator.snapshot()["mission"])

    def test_every_connected_uav_needs_gps_even_in_fixed_dalian_scene(self):
        with tempfile.TemporaryDirectory() as data_directory:
            app = create_app(dict(os.environ, COMPETITION_ADAPTER="distributed",
                                  COMPETITION_DATA_DIR=data_directory,
                                  COMPETITION_LOCAL_UAV_ID="1", COMPETITION_GROUND_PEERS=""))
            app.state.adapter.connected_uav_ids_snapshot = Mock(return_value=[1, 2])
            app.state.orchestrator.update_telemetry(Telemetry(
                uav_id=1, received_at=time.time(), connected=True,
                latitude=39.050245, longitude=121.661123,
                gps_status=3, location_source=4,
            ))
            response = TestClient(app).post("/api/v1/planning/competition-coverage", json={
                "subject": "subject1", "coordinate_mode": "gps", "flight_profile": "dalian_nanshan",
            })
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("UAV2", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
