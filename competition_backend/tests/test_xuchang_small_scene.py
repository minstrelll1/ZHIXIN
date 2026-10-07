"""许昌固定 GPS 场景：扣除区、五米间距、六机进返场和高度合同。"""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from competition_backend.api import create_app
from competition_backend import polygon_coverage as coverage
from competition_backend.orchestrator import _altitude_profile_for
from competition_backend.transit_routes import CACHE, attach_routes, clearance_region, rebase_gps_routes_for_takeoff, scene_plan
from competition_backend.xuchang_small_scene import AREA_PATH, load_xuchang_small_plan


class XuchangSmallSceneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_xuchang_small_plan()
        cls.area = cls.plan["search_area"]
        cls.saved = json.loads(CACHE.read_text(encoding="utf-8"))["scenes"]["xuchang_small/fixed_xuchang"]
        cls.hole = Polygon(cls.area["excluded_polygons_m"][0])
        cls.flyable = Polygon(cls.area["points_m"], holes=cls.area["excluded_polygons_m"])

    def test_fixed_geometry_and_coverage_swap_uav1_and_uav4(self):
        area = self.area
        source = json.loads(AREA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(area["points"], [[lat, lon] for lon, lat in source["boundary_lon_lat"]])
        self.assertEqual(area["departure_point_wgs84"], {"latitude": 34.138282, "longitude": 113.909077})
        self.assertTrue(self.flyable.is_valid)
        self.assertTrue(self.flyable.contains(Point(0, 0)))
        self.assertTrue(Polygon(area["points_m"]).contains(self.hole))
        regions, disks = [], []
        for uid in range(1, 7):
            task = self.plan["planned_uavs"][str(uid)]["task"]
            region = Polygon(task["polygon_m"])
            regions.append(region)
            self.assertLess(region.difference(self.flyable).area, 1e-5)
            self.assertEqual(len(task["waypoints_m"]), task["scan_count"])
            for waypoint in task["waypoints_m"]:
                self.assertGreaterEqual(Point(waypoint).distance(self.flyable.boundary), 5.0)
            for a, b in zip(task["waypoints_m"], task["waypoints_m"][1:]):
                segment = LineString([a, b])
                self.assertTrue(self.flyable.covers(segment))
                self.assertGreaterEqual(segment.distance(self.flyable.boundary), 5.0)
            disks.extend(Point(p).buffer(74.98, quad_segs=32) for p in task["waypoints_m"])
            self.assertLess(region.difference(unary_union(disks)).area, 1e-5)
        self.assertLess(self.flyable.symmetric_difference(unary_union(regions)).area, 1e-5)
        self.assertLess(self.flyable.difference(unary_union(disks)).area, 1e-5)
        near = [regions[i].distance(Point(0, 0)) for i in (0, 4, 5)]
        far = [regions[i].distance(Point(0, 0)) for i in (1, 2, 3)]
        self.assertGreater(min(far), max(near))
        self.assertEqual([self.plan["planned_uavs"][str(i)]["task"]["scan_count"]
                          for i in (1, 4)], [5, 3])

    def test_entry_and_all_fleet_return_routes_detour_around_hole(self):
        safe = clearance_region(self.area["points_m"], 5.0, holes=self.area["excluded_polygons_m"])
        self.assertEqual(len(safe.interiors), 1)
        home = self.saved["departure_m"]
        all_points = sum(len(item["waypoints_m"]) for item in self.saved["vehicles"].values())
        self.assertEqual(all_points, self.area["coverage"]["total_scan_count"])
        detours = 0
        for uid, vehicle in self.saved["vehicles"].items():
            self.assertEqual(vehicle["entry_path_m"][0], home)
            self.assertEqual(vehicle["entry_path_m"][-1], vehicle["waypoints_m"][0])
            self.assertEqual(len(vehicle["return_paths_m"]), len(vehicle["waypoints_m"]))
            for route in [vehicle["entry_path_m"], *vehicle["return_paths_m"]]:
                self.assertTrue(safe.buffer(1e-7).covers(LineString(route)))
                self.assertGreaterEqual(LineString(route).distance(self.flyable.boundary), 5)
                self.assertTrue(all(self.flyable.covers(Point(p)) for p in route))
            for point, path in zip(vehicle["waypoints_m"], vehicle["return_paths_m"]):
                self.assertEqual(path[0], point)
                self.assertEqual(path[-1], home)
                if self.hole.intersects(LineString([point, home])):
                    self.assertGreater(len(path), 2)
                    detours += 1
        self.assertGreater(detours, 0)
        self.assertEqual(self.saved["boundary_clearance_m"], 5.0)
        self.assertEqual(self.saved["excluded_polygons_m"], self.area["excluded_polygons_m"])

    def test_gps_live_rebase_stays_clear_and_rejects_home_in_margin(self):
        planned = attach_routes(scene_plan("xuchang_small", "fixed_xuchang"))
        area = planned["search_area"]
        safe = clearance_region(area["points_m"], 5.0, holes=area["excluded_polygons_m"])
        projection = area["coverage"]["projection"]
        home = {"latitude": 34.138312, "longitude": 113.909107}
        for uid in range(1, 7):
            task = planned["planned_uavs"][str(uid)]["task"]
            result = rebase_gps_routes_for_takeoff(area, task, home)
            self.assertEqual(result["departure"], [home["longitude"], home["latitude"]])
            self.assertEqual(len(result["return_paths"]), 31)
            self.assertEqual({part["source_uav_id"] for part in result["return_paths"]}, set(range(1, 7)))
            for path in [result["entry_path"], *(part["path"] for part in result["return_paths"])]:
                local = [[(p[1] - projection["latitude"]) * projection["north_m_per_degree"],
                          -(p[0] - projection["longitude"]) * projection["west_m_per_degree"]] for p in path]
                self.assertTrue(safe.buffer(2e-4).covers(LineString(local)))
                self.assertGreaterEqual(LineString(local).distance(self.flyable.boundary), 5.0)
        with self.assertRaisesRegex(ValueError, "不足 5 米"):
            rebase_gps_routes_for_takeoff(area, planned["planned_uavs"]["1"]["task"],
                                          {"latitude": 34.1378, "longitude": 113.9108})

    def test_cache_is_read_only_and_api_dispatch_selects_new_altitudes(self):
        with patch("competition_backend.xuchang_small_scene.prepare_xuchang_small_plan",
                   side_effect=AssertionError("运行时不应重新规划")):
            self.assertEqual(load_xuchang_small_plan()["search_area"]["flight_profile"], "xuchang_small")
        with self.assertRaises(ValueError):
            load_xuchang_small_plan(coordinate_mode="xyz")
        with tempfile.TemporaryDirectory() as data_dir:
            app = create_app({"COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": data_dir,
                              "COMPETITION_DIAGNOSTICS_DIR": data_dir})
            try:
                client = TestClient(app)
                payload = {"subject": "subject1", "planning_mode": "competition",
                           "flight_profile": "xuchang_small", "coordinate_mode": "gps",
                           "departure_point": "fixed_xuchang", "controller_mode": "external",
                           "flight_altitude_plan": "around54m",
                           "gps_origin": {"latitude": 31.0, "longitude": 101.0}}
                preview = client.post("/api/v1/planning/competition-coverage", json=payload)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["search_area"]["gps_origin"]["latitude"], 34.138282)
                response = client.post("/api/v1/plan", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                mission = response.json()["mission"]
                self.assertEqual([mission["uavs"][str(i)]["target_altitude_m"] for i in range(1, 7)],
                                 [64.0, 60.0, 56.0, 52.0, 48.0, 44.0])
                self.assertEqual([entry["payload"]["target_altitude_m"] for entry in app.state.adapter.snapshot()
                                  if entry["type"] == "assign_task"],
                                 [64.0, 60.0, 56.0, 52.0, 48.0, 44.0])
                invalid = client.post("/api/v1/planning/competition-coverage",
                                      json={**payload, "coordinate_mode": "xyz"})
                self.assertEqual(invalid.status_code, 422)
            finally:
                app.state.audit.close()

    def test_new_altitude_choice_resolves_for_every_scene(self):
        for profile in ("lab", "outdoor5", "lab10", "outdoor100", "outdoor200",
                        "competition", "dalian_nanshan", "xuchang_small"):
            self.assertEqual(_altitude_profile_for(profile, "around54m"), "around54m")
