"""Regression checks for saved offline plans and scaled GPS coordinates."""

import os
import tempfile
import unittest
from unittest.mock import patch

from competition_backend import polygon_coverage
from competition_backend.api import create_app


class OfflineCoverageProjectionTest(unittest.TestCase):
    def test_scaled_gps_points_use_actual_anchor_latitude(self):
        prepared = polygon_coverage.plan_competition_coverage()

        for latitude, longitude in ((30.78528, 103.86102), (60.12345, 10.54321)):
            with self.subTest(latitude=latitude):
                result = polygon_coverage.adapt_competition_plan(
                    prepared,
                    coordinate_mode="gps",
                    flight_profile="outdoor5",
                    max_extent_m=5.0,
                    gps_origin={"latitude": latitude, "longitude": longitude},
                )
                area = result["search_area"]
                _, projection = polygon_coverage.local_projection(
                    [(latitude, longitude), (latitude, longitude)]
                )
                self.assertLessEqual(area["width_m"], 5.0)
                self.assertLessEqual(area["height_m"], 5.0)

                samples = list(zip(area["points_m"], area["points"]))
                for wrapper in result["planned_uavs"].values():
                    task = wrapper["task"]
                    samples.extend(zip(task["waypoints_m"], task["waypoints_wgs84"]))
                self.assertTrue(any(abs(point[1]) > 1.0 for point, _ in samples))

                for point, gps in samples:
                    expected_latitude = latitude + point[0] / projection["north_m_per_degree"]
                    expected_longitude = longitude - point[1] / projection["west_m_per_degree"]
                    self.assertAlmostEqual(gps[0], expected_latitude, delta=1e-9)
                    self.assertAlmostEqual(gps[1], expected_longitude, delta=1e-9)

    def test_offline_competition_plan_loads_without_solver_or_uav(self):
        with patch.object(
            polygon_coverage, "_compute", side_effect=AssertionError("runtime solver was called")
        ):
            prepared = polygon_coverage.plan_competition_coverage()
            self.assertEqual(len(prepared["planned_uavs"]), 6)
            self.assertEqual(prepared["prepared_plan"]["runtime_recalculation"], False)

            with tempfile.TemporaryDirectory() as data_directory, patch.dict(
                os.environ,
                {
                    "COMPETITION_ADAPTER": "distributed",
                    "COMPETITION_LOCAL_UAV_ID": "1",
                    "COMPETITION_GROUND_PEERS": "",
                    "COMPETITION_DATA_DIR": data_directory,
                },
                clear=False,
            ):
                app = create_app()
                endpoint = next(
                    route.endpoint for route in app.routes if route.path == "/api/v1/plan"
                )
                self.assertEqual(app.state.adapter.connected_uav_ids_snapshot(), [])
                result = endpoint(
                    {
                        "subject": "subject1",
                        "planning_mode": "competition",
                        "coordinate_mode": "gps",
                        "flight_profile": "outdoor5",
                        "controller_mode": "external",
                        "gps_origin": {"latitude": 30.78528, "longitude": 103.86102},
                    }
                )

                self.assertEqual(result["dispatch_status"]["mode"], "planning_only")
                self.assertEqual(result["dispatch_status"]["assigned_uav_ids"], [])
                self.assertEqual(result["active_uav_ids"], [])
                self.assertEqual(result["mission"]["uavs"], {})
                self.assertEqual(len(result["mission"]["planned_uavs"]), 6)
                self.assertFalse(app.state.adapter.is_coordinator)


if __name__ == "__main__":
    unittest.main()
