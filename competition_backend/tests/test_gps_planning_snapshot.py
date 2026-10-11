"""Planning GPS checks keep valid fixes without serializing an existing mission."""

import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from competition_backend.api import _fresh_onboard_gps_reference, create_app
from competition_backend.models import Telemetry


class GpsPlanningSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit = Mock()
        self.app = create_app({
            "COMPETITION_ADAPTER": "distributed",
            "COMPETITION_DATA_DIR": self.directory.name,
            "COMPETITION_LOCAL_UAV_ID": "2",
            "COMPETITION_GROUND_PEERS": "",
        }, audit=self.audit)
        self.backend = self.app.state.orchestrator
        self.adapter = self.app.state.adapter
        self.adapter.connected_uav_ids_snapshot = Mock(return_value=[1, 2, 4, 5])
        self.adapter.assign_task = Mock(side_effect=AssertionError("preview must not dispatch"))
        self.client = TestClient(self.app)  # No lifespan: no sockets or background workers.
        self.payload = {
            "subject": "subject1", "coordinate_mode": "gps",
            "flight_profile": "dalian_nanshan",
        }
        self.now = patch("competition_backend.api.time.time", return_value=1000.0)
        self.now.start()
        self.addCleanup(self.now.stop)
        for uid in (1, 2, 4, 5):
            self.update(uid)

    def update(self, uid, **fields):
        values = dict(
            uav_id=uid, received_at=999.8, connected=True,
            latitude=39.050245 + uid * 0.000001, longitude=121.661123,
            gps_status=3, location_source=4, gps_telemetry_age_seconds=0.1,
        )
        values.update(fields)
        self.backend.update_telemetry(Telemetry(**values))

    def preview(self):
        # Geometry is unrelated to GPS admission and already has its own tests.
        with patch("competition_backend.fixed_gps_scene.load_dalian_nanshan_plan",
                   side_effect=lambda **kw: {"search_area": {"coordinate_mode": "gps"}}), \
                patch("competition_backend.transit_routes.attach_routes", side_effect=lambda plan: plan):
            return self.client.post("/api/v1/planning/competition-coverage", json=self.payload)

    def test_second_preview_reads_current_telemetry_without_serializing_old_mission(self):
        first = self.preview()
        self.assertEqual(first.status_code, 200, first.text)
        old_mission = Mock()
        old_mission.to_dict.side_effect = AssertionError("GPS must not serialize the old mission")
        self.backend._mission = old_mission
        self.backend.snapshot = Mock(side_effect=AssertionError("GPS must use telemetry-only snapshot"))
        self.update(4, latitude=39.050999)

        second = self.preview()

        self.assertEqual(second.status_code, 200, second.text)
        references = second.json()["search_area"]["takeoff_gps_by_uav"]
        self.assertEqual(set(references), {"1", "2", "4", "5"})
        self.assertEqual(references["4"]["latitude"], 39.050999)
        self.backend.snapshot.assert_not_called()
        old_mission.to_dict.assert_not_called()
        self.assertIs(self.backend._mission, old_mission)
        self.adapter.assign_task.assert_not_called()

    def test_second_preview_accepts_delayed_gps_without_requiring_operator_retry(self):
        self.assertEqual(self.preview().status_code, 200)
        for uid in (1, 4, 5):
            self.update(uid, gps_telemetry_age_seconds=60.0)
        self.audit.record.reset_mock()
        response = self.preview()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(set(response.json()["search_area"]["takeoff_gps_by_uav"]), {"1", "2", "4", "5"})
        self.assertFalse([c for c in self.audit.record.call_args_list if c.args == ("规划 GPS 校验未通过",)])
        self.adapter.assign_task.assert_not_called()

    def test_precise_gps_is_not_discarded_due_to_age(self):
        for age in (2.03, 60, 3600):
            self.update(1, gps_telemetry_age_seconds=age,
                        gps_position={"latitude": 39.123456789, "longitude": 121.123456789, "age_seconds": age})
            reference = _fresh_onboard_gps_reference(self.backend.telemetry_snapshot()["1"], 1000., 2., 1)
            self.assertEqual(reference["source"], "mavros_global")
            self.assertEqual(reference["latitude"], 39.123456789)

    def test_rejection_diagnostics_preserve_validity_guards(self):
        cases = (
            ({"gps_status": 2}, "invalid_gps_status"),
            ({"location_source": 2}, "invalid_location_source"),
            ({"latitude": None}, "no_valid_coordinates"),
        )
        for changes, expected_reason in cases:
            with self.subTest(changes=changes):
                self.update(1, **changes)
                item = self.backend.telemetry_snapshot()["1"]
                details = {}
                reference = _fresh_onboard_gps_reference(
                    item, 1000.0, 2.0, 1, rejection_details=details)
                self.assertIsNone(reference)
                self.assertEqual(details["reason"], expected_reason)
                self.assertIsNone(details["max_age_seconds"])
                self.assertFalse(details["gps_age_gate_enabled"])


if __name__ == "__main__":
    unittest.main()
