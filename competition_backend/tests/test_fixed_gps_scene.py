import math
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from competition_backend.api import create_app
from competition_backend import polygon_coverage
from competition_backend.fixed_gps_scene import (
    AREA_PATH,
    PROFILE,
    load_dalian_nanshan_plan,
)


class DalianFixedSceneTest(unittest.TestCase):
    def test_saved_wgs84_area_and_six_routes_are_complete(self):
        with patch("competition_backend.fixed_gps_scene.prepare_dalian_nanshan_plan",
                   side_effect=AssertionError("运行时不得重新规划")):
            plan = load_dalian_nanshan_plan()
        area = plan["search_area"]
        self.assertEqual(area["flight_profile"], PROFILE)
        self.assertEqual(area["coordinate_mode"], "gps")
        self.assertEqual(area["departure_point"], "fixed_dalian")
        self.assertEqual(area["departure_point_m"], [0.0, 0.0])
        self.assertEqual(area["departure_point_wgs84"], {
            "latitude": 39.050245, "longitude": 121.661123,
        })
        self.assertEqual(area["landing_mode"], "onboard_home")
        self.assertEqual(area["coverage"]["reconnaissance_radius_m"], 75.0)
        self.assertEqual(area["coverage"]["speed_mps"], 5.0)
        self.assertEqual(area["coverage"]["hover_scan_seconds"], 10.0)
        self.assertFalse(plan["prepared_plan"]["runtime_recalculation"])
        self.assertTrue(AREA_PATH.exists())

        boundary = Polygon(area["points_m"])
        self.assertTrue(boundary.is_valid)
        self.assertEqual(len(plan["planned_uavs"]), 6)
        self.assertLessEqual(area["origin_x_m"], 0)
        self.assertLessEqual(area["origin_y_m"], 0)
        self.assertGreaterEqual(area["origin_x_m"] + area["height_m"], boundary.bounds[2])
        self.assertGreaterEqual(area["origin_y_m"] + area["width_m"], boundary.bounds[3])
        regions = []
        total_time = total_distance = 0.0
        total_scans = 0
        projection = area["coverage"]["projection"]
        for item in plan["planned_uavs"].values():
            task = item["task"]
            region = Polygon(task["polygon_m"])
            regions.append(region)
            self.assertTrue(boundary.buffer(1e-6).covers(region))
            self.assertGreater(task["scan_count"], 0)
            self.assertEqual(task["scan_count"], len(task["waypoints_m"]))
            self.assertEqual(task["scan_count"], len(task["waypoints_wgs84"]))
            self.assertEqual(task["flight_path_m"][0], [0.0, 0.0])
            self.assertEqual(task["flight_path_m"][-1], [0.0, 0.0])
            for a, b in zip(task["flight_path_m"][1:-2], task["flight_path_m"][2:-1]):
                if region.buffer(1e-6).covers(Point(a)) and region.buffer(1e-6).covers(Point(b)):
                    self.assertTrue(region.buffer(1e-6).covers(LineString([a, b])))
            disks = unary_union([Point(point).buffer(74.98, quad_segs=32)
                                 for point in task["waypoints_m"]])
            self.assertLess(region.difference(disks).area, 1e-5)
            for point, gps in zip(task["waypoints_m"], task["waypoints_wgs84"]):
                self.assertTrue(region.buffer(1e-6).covers(Point(point)))
                self.assertAlmostEqual(gps[0], projection["latitude"] + point[0] / projection["north_m_per_degree"], places=9)
                self.assertAlmostEqual(gps[1], projection["longitude"] - point[1] / projection["west_m_per_degree"], places=9)
            distance = sum(math.dist(a, b) for a, b in zip(task["flight_path_m"], task["flight_path_m"][1:]))
            self.assertAlmostEqual(distance, task["route_distance_m"], places=5)
            self.assertAlmostEqual(task["mission_time_s"], distance / 5 + task["scan_count"] * 10, places=5)
            total_time += task["mission_time_s"]
            total_distance += distance
            total_scans += task["scan_count"]
        combined = unary_union(regions)
        self.assertLess(boundary.symmetric_difference(combined).area, 1e-5)
        self.assertLess(sum(region.area for region in regions) - combined.area, 1e-5)
        self.assertAlmostEqual(area["coverage"]["total_mission_time_s"], total_time, places=4)
        self.assertAlmostEqual(area["coverage"]["total_distance_m"], total_distance, places=4)
        self.assertEqual(area["coverage"]["total_scan_count"], total_scans)

    def test_only_fixed_gps_snapshot_is_accepted(self):
        with self.assertRaises(ValueError):
            load_dalian_nanshan_plan(coordinate_mode="xyz")
        with patch("competition_backend.fixed_gps_scene._source_digest", return_value="stale"):
            with self.assertRaises(polygon_coverage.PlanNotPreparedError):
                load_dalian_nanshan_plan()

    def test_preview_and_dispatch_use_fixed_coordinates_and_selected_altitude(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": directory,
        }, clear=False):
            app = create_app()
            client = TestClient(app)
            payload = {
                "subject": "subject1", "planning_mode": "competition",
                "flight_profile": PROFILE, "coordinate_mode": "gps",
                "departure_point": "fixed_dalian", "controller_mode": "external",
                "flight_altitude_plan": "around2m",
                # A live aircraft or an obsolete UI value must not relocate this scene.
                "gps_origin": {"latitude": 30.78528, "longitude": 103.86102},
            }
            with patch("competition_backend.fixed_gps_scene.prepare_dalian_nanshan_plan",
                       side_effect=AssertionError("运行时不得重新规划")):
                preview = client.post("/api/v1/planning/competition-coverage", json=payload)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["search_area"]["gps_origin"]["latitude"], 39.050245)
                self.assertEqual(client.get("/api/v1/status").json()["mission"], None)
                response = client.post("/api/v1/plan", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            mission = response.json()["mission"]
            self.assertEqual(mission["search_area"]["gps_origin"]["longitude"], 121.661123)
            self.assertEqual(mission["flight_profile"], PROFILE)
            self.assertEqual(mission["controller_mode"], "external")
            self.assertEqual([mission["uavs"][str(uid)]["target_altitude_m"]
                              for uid in range(1, 7)], [1.5, 2.0, 2.5, 1.5, 2.0, 2.5])
            for uid in range(1, 7):
                self.assertEqual(mission["uavs"][str(uid)]["task"]["waypoints_wgs84"],
                                 preview.json()["planned_uavs"][str(uid)]["task"]["waypoints_wgs84"])
            assigned = [entry for entry in app.state.adapter.snapshot()
                        if entry["type"] == "assign_task"]
            self.assertEqual(len(assigned), 6)
            self.assertEqual([entry["payload"]["target_altitude_m"] for entry in assigned],
                             [1.5, 2.0, 2.5, 1.5, 2.0, 2.5])

            invalid = client.post("/api/v1/planning/competition-coverage", json={
                **payload, "coordinate_mode": "xyz",
            })
            self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
