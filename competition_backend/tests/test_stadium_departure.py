"""操场中央赛前方案的缓存、分区、覆盖与坐标回归。"""
import math
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union

from competition_backend import polygon_coverage as coverage
from competition_backend import stadium_departure as stadium
from competition_backend.api import create_app


class StadiumDepartureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = coverage.plan_competition_coverage()
        cls.plan = stadium.load_prepared_stadium_plan(cls.base)

    def test_default_plan_is_unmodified(self):
        self.assertEqual(
            coverage._plan_hash(self.base),
            "dd01e21465528e82d3efadb1f84212c4fe43a6ac0e1b93ee9c1bfa654fdd2589",
        )
        self.assertNotIn("departure_point", self.base["search_area"])

    def test_runtime_load_never_solves_and_rejects_changed_source(self):
        with patch.object(stadium, "prepare_stadium_center_plan", side_effect=AssertionError("运行时不得求解")):
            self.assertEqual(
                stadium.load_prepared_stadium_plan(self.base)["search_area"]["departure_point"],
                "stadium_center",
            )
        changed = dict(self.base)
        changed["subject"] = "changed"
        with self.assertRaises(coverage.PlanNotPreparedError):
            stadium.load_prepared_stadium_plan(changed)

    def test_repartition_and_round_trips_cover_required_area(self):
        area = self.plan["search_area"]
        polygon = Polygon(area["points_m"])
        required = shape(area["terrain_layers_m"]["required"])
        depot = area["departure_point_m"]
        self.assertTrue(polygon.covers(Point(depot)))
        regions = []
        all_times = []
        all_distances = []
        for item in self.plan["planned_uavs"].values():
            task = item["task"]
            region = Polygon(task["polygon_m"])
            regions.append(region)
            self.assertTrue(region.is_valid)
            self.assertLessEqual(coverage.shape_metrics(region)["aspect_ratio"], 2.000001)
            scans = task["scan_waypoints_m"]
            self.assertTrue(all(region.buffer(1e-6).covers(Point(point)) for point in scans))
            covered = unary_union([Point(point).buffer(74.98, quad_segs=32) for point in scans])
            self.assertLess(required.intersection(region).difference(covered).area, 1e-5)
            flight = task["flight_path_m"]
            self.assertLess(math.dist(flight[0], depot), 1e-6)
            self.assertLess(math.dist(flight[-1], depot), 1e-6)
            self.assertTrue(polygon.buffer(1e-6).covers(LineString(flight)))
            distance = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
            self.assertAlmostEqual(distance, task["route_distance_m"], places=6)
            self.assertAlmostEqual(task["mission_time_s"], distance / 5 + task["scan_count"] * 10, places=6)
            all_times.append(task["mission_time_s"])
            all_distances.append(distance)
        self.assertLess(polygon.symmetric_difference(unary_union(regions)).area, 1e-5)
        self.assertLess(sum(region.area for region in regions) - unary_union(regions).area, 1e-5)
        self.assertLess(max(all_times) - min(all_times), 20.0)
        self.assertAlmostEqual(area["coverage"]["completion_time_spread_s"], max(all_times) - min(all_times))
        self.assertAlmostEqual(area["coverage"]["total_distance_m"], sum(all_distances))
        self.assertEqual(area["landing_mode"], "onboard_home")
        self.assertFalse(area["coverage"]["global_optimum_proven"])

    def test_competition_gps_anchor_and_scaled_xyz(self):
        competition = stadium.anchor_stadium_plan(
            coverage.adapt_competition_plan(self.plan, "gps", "competition"), self.plan,
        )
        area = competition["search_area"]
        self.assertEqual(area["departure_point_m"], [0.0, 0.0])
        self.assertEqual(area["gps_origin"], stadium.STADIUM_CENTER_WGS84)
        self.assertEqual(area["landing_mode"], "onboard_home")
        for item in competition["planned_uavs"].values():
            flight = item["task"]["flight_path_m"]
            self.assertEqual(flight[0], [0.0, 0.0])
            self.assertEqual(flight[-1], [0.0, 0.0])
        competition_xyz = stadium.anchor_stadium_plan(
            coverage.adapt_competition_plan(self.plan, "xyz", "competition"), self.plan,
        )
        self.assertEqual(competition_xyz["search_area"]["departure_point_m"], [0.0, 0.0])
        self.assertNotEqual(competition_xyz["search_area"]["terrain_layers_m"],
                            self.plan["search_area"]["terrain_layers_m"])
        for item in competition_xyz["planned_uavs"].values():
            self.assertGreater(len(item["task"]["waypoints_m"]), 0)
            self.assertEqual(item["task"]["flight_path_m"][0], [0.0, 0.0])
            self.assertEqual(item["task"]["flight_path_m"][-1], [0.0, 0.0])
        laboratory_source = stadium.load_prepared_stadium_plan(self.base, "lab", 1.0, 0.2)
        laboratory = stadium.anchor_stadium_plan(
            coverage.adapt_competition_plan(
                laboratory_source, "xyz", "lab", max_extent_m=3.0,
                lab_radius_m=1.0, lab_speed_mps=0.2,
            ), laboratory_source,
        )
        self.assertEqual(laboratory["search_area"]["departure_point_m"], [0.0, 0.0])
        self.assertEqual(laboratory["search_area"]["points"], laboratory["search_area"]["points_m"])
        times = [item["task"]["mission_time_s"] for item in laboratory["planned_uavs"].values()]
        self.assertAlmostEqual(laboratory["search_area"]["coverage"]["completion_time_spread_s"], max(times) - min(times))

    def test_each_scaled_scene_has_cached_xyz_and_gps_routes(self):
        references = {str(uid): {"latitude": 30.78528, "longitude": 103.86102}
                      for uid in range(1, 7)}
        for profile, extent, speed in (("lab", 3.0, 0.2), ("outdoor5", 5.0, 0.2),
                                       ("lab10", 10.0, 0.5)):
            source = stadium.load_prepared_stadium_plan(self.base, profile, 1.0, speed)
            for mode in ("xyz", "gps"):
                with self.subTest(profile=profile, mode=mode):
                    adapted = coverage.adapt_competition_plan(
                        source, coordinate_mode=mode, flight_profile=profile,
                        max_extent_m=extent, lab_radius_m=1.0, lab_speed_mps=speed,
                        gps_origins_by_uav=references if mode == "gps" else None,
                    )
                    plan = stadium.anchor_stadium_plan(adapted, source)
                    area = plan["search_area"]
                    self.assertEqual(area["departure_point_m"], [0.0, 0.0])
                    self.assertEqual(area["landing_mode"], "onboard_home")
                    self.assertLessEqual(max(area["height_m"], area["width_m"]), extent + 1e-6)
                    times = []
                    for item in plan["planned_uavs"].values():
                        task = item["task"]
                        self.assertGreater(task["scan_count"], 0)
                        self.assertEqual(task["flight_path_m"][0], [0.0, 0.0])
                        self.assertEqual(task["flight_path_m"][-1], [0.0, 0.0])
                        self.assertEqual(len(task["waypoints_m"]), task["scan_count"])
                        if mode == "gps":
                            self.assertEqual(len(task["waypoints_wgs84"]), task["scan_count"])
                        times.append(task["mission_time_s"])
                    self.assertAlmostEqual(area["coverage"]["completion_time_spread_s"], max(times) - min(times))
                    if mode == "gps":
                        self.assertEqual(area["terrain_layers_m"], source["search_area"]["terrain_layers_m"])
                    else:
                        self.assertNotEqual(area["terrain_layers_m"], source["search_area"]["terrain_layers_m"])

    def test_preview_and_dispatch_use_same_cached_stadium_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(dict(os.environ, COMPETITION_ADAPTER="sim", COMPETITION_DATA_DIR=folder))
            client = TestClient(app)
            payload = {
                "subject": "subject2", "coordinate_mode": "xyz",
                "flight_profile": "lab", "departure_point": "stadium_center",
            }
            with patch.object(stadium, "prepare_stadium_center_plan", side_effect=AssertionError("运行时不得求解")):
                preview = client.post("/api/v1/planning/competition-coverage", json=payload)
                self.assertEqual(preview.status_code, 200, preview.text)
                dispatch = client.post(
                    "/api/v1/plan",
                    json=dict(payload, planning_mode="competition", controller_mode="external"),
                )
            self.assertEqual(dispatch.status_code, 200, dispatch.text)
            mission = dispatch.json()["mission"]
            self.assertEqual(mission["search_area"]["departure_point"], "stadium_center")
            self.assertEqual(mission["search_area"]["landing_mode"], "onboard_home")
            self.assertEqual(mission["prepared_plan"]["id"], preview.json()["prepared_plan"]["id"])
            self.assertEqual(mission["planned_uavs"]["1"]["task"]["waypoints_m"],
                             preview.json()["planned_uavs"]["1"]["task"]["waypoints_m"])
            self.assertIsNone(mission["planned_uavs"]["1"]["landing_point_m"])


if __name__ == "__main__":
    unittest.main()
