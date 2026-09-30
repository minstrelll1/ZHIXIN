"""Regression checks for saved offline plans and scaled GPS coordinates."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from competition_backend import polygon_coverage, scaled_scene_plans, stadium_departure
from competition_backend.api import create_app


class OfflineCoverageProjectionTest(unittest.TestCase):
    def test_prepared_outdoor_scenes_plan_without_a_connected_uav_or_solver(self):
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
            from fastapi.testclient import TestClient

            client = TestClient(app)
            endpoint = next(route.endpoint for route in app.routes if route.path == "/api/v1/plan")
            self.assertEqual(app.state.adapter.connected_uav_ids_snapshot(), [])

            with ExitStack() as mocks:
                mocks.enter_context(patch.object(
                    polygon_coverage, "_compute", side_effect=AssertionError("runtime solver was called")))
                mocks.enter_context(patch.object(
                    scaled_scene_plans, "prepare_scaled_scene_plan",
                    side_effect=AssertionError("runtime scaled solver was called")))
                mocks.enter_context(patch.object(
                    stadium_departure, "prepare_stadium_center_plan",
                    side_effect=AssertionError("runtime stadium solver was called")))
                mocks.enter_context(patch.object(
                    stadium_departure, "_candidate_route",
                    side_effect=AssertionError("runtime route solver was called")))
                for profile, extent in (("outdoor100", 100.0), ("outdoor200", 200.0)):
                    for departure in ("southeast", "stadium_center"):
                        for coordinate in ("xyz", "gps"):
                            with self.subTest(profile=profile, departure=departure, coordinate=coordinate):
                                payload = {
                                    "subject": "subject1",
                                    "flight_profile": profile,
                                    "departure_point": departure,
                                    "coordinate_mode": coordinate,
                                    "controller_mode": "external",
                                }
                                if coordinate == "gps":
                                    payload["gps_origin"] = {
                                        "latitude": 30.78528,
                                        "longitude": 103.86102,
                                        "altitude_m": 100.0,
                                    }
                                preview_response = client.post(
                                    "/api/v1/planning/competition-coverage", json=payload
                                )
                                self.assertEqual(preview_response.status_code, 200, preview_response.text)
                                preview = preview_response.json()
                                self.assertTrue(preview["preview_only"])

                                result = endpoint(dict(payload, planning_mode="competition"))
                                mission = result["mission"]
                                area = mission["search_area"]
                                self.assertEqual(result["dispatch_status"]["mode"], "planning_only")
                                self.assertEqual(result["dispatch_status"]["assigned_uav_ids"], [])
                                self.assertEqual(result["active_uav_ids"], [])
                                self.assertEqual(mission["uavs"], {})
                                self.assertEqual(len(mission["planned_uavs"]), 6)
                                self.assertEqual(mission["prepared_plan"]["id"], preview["prepared_plan"]["id"])
                                self.assertFalse(mission["prepared_plan"]["runtime_recalculation"])
                                self.assertLessEqual(max(area["width_m"], area["height_m"]), extent + 1e-6)
                                self.assertEqual(area["departure_point"], departure)
                                self.assertEqual(area["departure_point_m"], [0.0, 0.0])
                                self.assertEqual(area["landing_mode"], "onboard_home")
                                self.assertEqual(area["coordinate_mode"], coordinate)
                                for uav_id, wrapper in mission["planned_uavs"].items():
                                    task = wrapper["task"]
                                    if task["scan_count"]:
                                        self.assertEqual(task["flight_path_m"][0], [0.0, 0.0])
                                        self.assertEqual(task["flight_path_m"][-1], [0.0, 0.0])
                                    else:
                                        self.assertEqual(task["flight_path_m"], [])
                                        self.assertEqual(task["mission_time_s"], 0.0)
                                    self.assertEqual(task["waypoints_m"], preview["planned_uavs"][uav_id]["task"]["waypoints_m"])
                                    self.assertIsNone(wrapper["landing_point_m"])
                                    if coordinate == "gps":
                                        self.assertEqual(task["coordinate_frame"], "LOCAL_NORTH_WEST")
                                        self.assertEqual(len(task["waypoints_wgs84"]), task["scan_count"])
                                    else:
                                        self.assertEqual(task["coordinate_frame"], "ENU")
                                        self.assertNotIn("waypoints_wgs84", task)
                                if coordinate == "gps":
                                    self.assertEqual(area["coordinate_frame"], "WGS84")
                                    self.assertEqual(area["gps_origin"]["latitude"], 30.78528)
                                    self.assertEqual(area["gps_origin"]["longitude"], 103.86102)
                                else:
                                    self.assertEqual(area["coordinate_frame"], "ENU")
                                    self.assertEqual(area["points"], area["points_m"])

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
