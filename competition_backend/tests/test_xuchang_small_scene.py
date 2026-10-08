"""许昌固定 GPS 场景：扣除区、五米间距、六机进返场和高度合同。"""
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

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

    def test_fixed_geometry_and_45m_coverage(self):
        area = self.area
        source = json.loads(AREA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(area["points"], [[lat, lon] for lon, lat in source["boundary_lon_lat"]])
        self.assertEqual(area["departure_point_wgs84"], {"latitude": 34.138282, "longitude": 113.909077})
        self.assertTrue(self.flyable.is_valid)
        self.assertTrue(self.flyable.contains(Point(0, 0)))
        self.assertTrue(Polygon(area["points_m"]).contains(self.hole))
        self.assertEqual(area["coverage"]["reconnaissance_radius_m"], 45.0)
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
            self.assertEqual(task["reconnaissance_radius_m"], 45.0)
            own_disks = [Point(p).buffer(44.98, quad_segs=32) for p in task["waypoints_m"]]
            disks.extend(own_disks)
            self.assertLess(region.difference(unary_union(own_disks)).area, 1e-5)
        self.assertLess(self.flyable.symmetric_difference(unary_union(regions)).area, 1e-5)
        self.assertLess(self.flyable.difference(unary_union(disks)).area, 1e-5)


    def test_uav1_matches_requested_boundary_and_other_uavs_fill_remainder(self):
        points_lat_lon = [
            [34.13835116989908, 113.90969315353426],
            [34.13835116989908, 113.90860191266768],
            [34.1390373699571, 113.90861993970964],
            [34.139028715001785, 113.90968131590746],
        ]
        self.assertEqual(self.area["fixed_subregions_lon_lat"]["1"],
                         [[lon, lat] for lat, lon in points_lat_lon])
        projection = self.area["coverage"]["projection"]
        requested = Polygon([[(lat - projection["latitude"]) * projection["north_m_per_degree"],
                              -(lon - projection["longitude"]) * projection["west_m_per_degree"]]
                             for lat, lon in points_lat_lon])
        regions = {uid: Polygon(item["task"]["polygon_m"])
                   for uid, item in self.plan["planned_uavs"].items()}
        self.assertLess(regions["1"].symmetric_difference(requested).area, 1e-8)
        self.assertLess(requested.difference(self.flyable).area, 1e-8)
        for uid, region in regions.items():
            for other_uid, other in regions.items():
                if uid != other_uid:
                    self.assertLess(region.intersection(other).area, 1e-8)
        remainder = unary_union([region for uid, region in regions.items() if uid != "1"])
        self.assertLess(remainder.symmetric_difference(self.flyable.difference(requested)).area, 1e-5)
        for point in self.plan["planned_uavs"]["1"]["task"]["waypoints_m"]:
            # 分割线上的航点允许浮点几何误差；外边界和扣除区的 5 米间距另行严格校验。
            self.assertLessEqual(requested.distance(Point(point)), 1e-7)


    def test_uav4_is_requested_area_minus_new_exclusion_and_original_rectangle_is_removed(self):
        exclusion = [
            [34.139176470619766, 113.90981948390986],
            [34.13911894613526, 113.91127900740348],
            [34.13854369913638, 113.9112600525529],
            [34.13852278098991, 113.91014171636951],
            [34.13789000461156, 113.91004694211667],
            [34.13776449468414, 113.90978789249225],
        ]
        requested = [
            [34.13860949591008, 113.909810980447],
            [34.13726810338073, 113.90976575305758],
            [34.13723690795226, 113.91152208334715],
            [34.13860325692433, 113.91171806870135],
        ]
        projection = self.area["coverage"]["projection"]
        def project(point):
            lat, lon = point
            return [(lat - projection["latitude"]) * projection["north_m_per_degree"],
                    -(lon - projection["longitude"]) * projection["west_m_per_degree"]]
        self.assertEqual(self.area["excluded_points"], exclusion)
        self.assertEqual(self.area["requested_subregions_lon_lat"]["4"], [[lon, lat] for lat, lon in requested])
        region = Polygon(self.plan["planned_uavs"]["4"]["task"]["polygon_m"])
        uav1 = Polygon(self.plan["planned_uavs"]["1"]["task"]["polygon_m"])
        raw = Polygon([project(point) for point in requested])
        expected = raw.intersection(self.flyable).difference(uav1)
        self.assertTrue(region.is_valid)
        self.assertLess(region.symmetric_difference(expected).area, 1e-8)
        self.assertLess(region.intersection(self.hole).area, 1e-8)
        self.assertGreater(raw.intersection(self.hole).area, 3000)
        self.assertTrue(self.flyable.contains(Point(project([34.1376, 113.9117]))))
        others = [Polygon(item["task"]["polygon_m"]) for uid, item in self.plan["planned_uavs"].items()
                  if uid not in ("1", "4")]
        self.assertLess(unary_union(others).symmetric_difference(self.flyable.difference(region.union(uav1))).area, 1e-5)

    def test_all_altitudes_dispatch_same_new_geometry_and_own_landing_routes(self):
        from competition_backend.adapter import RecordingAdapter
        from competition_backend.config import load_config
        from competition_backend.orchestrator import CompetitionOrchestrator
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "src/su17_competition_executor/src"))
        from su17_competition_executor.transit_protocol import route_messages
        from su17_competition_executor.task_protocol import validate_assignment
        config = load_config(str(root / "competition_backend/config/competition.example.json"))
        expected = {"around1m": [.5, 1.5, .5, 1., 1.5, 1.],
                    "around2m": [1.5, 2., 2.5, 1.5, 2., 2.5],
                    "around5m": [4.5, 5., 5.5, 4.5, 5., 5.5],
                    "around10m": [8., 10., 12., 8., 10., 12.],
                    "around45m": [40., 50., 40., 45., 50., 45.],
                    "around54m": [61., 58., 55., 52., 49., 46.]}
        homes = {str(uid): {"latitude": 34.138282 + uid * .00001,
                           "longitude": 113.909077 + uid * .00001} for uid in range(1, 7)}
        for altitude, heights in expected.items():
            with self.subTest(altitude=altitude):
                prepared = attach_routes(load_xuchang_small_plan())
                prepared["search_area"]["takeoff_gps_by_uav"] = homes
                adapter = RecordingAdapter()
                planner = CompetitionOrchestrator(config, adapter, live_mode=False)
                planner.plan("subject1", flight_profile="xuchang_small", flight_altitude_plan=altitude,
                             controller_mode="external", prepared_plan=prepared)
                sent = [item for item in adapter.snapshot() if item["type"] == "assign_task"]
                self.assertEqual(len(sent), 6)
                for item, height in zip(sent, heights):
                    uid, assignment = str(item["uav_id"]), item["payload"]
                    task = assignment["task"]
                    home = homes[uid]
                    self.assertEqual(assignment["target_altitude_m"], height)
                    self.assertEqual(task["waypoints_wgs84"], self.plan["planned_uavs"][uid]["task"]["waypoints_wgs84"])
                    self.assertEqual(assignment["landing_point_wgs84"], [home["latitude"], home["longitude"], height])
                    message = dict(assignment, type="assign_task", uav_id=int(uid))
                    validate_assignment(message, int(uid))
                    entry, returns = route_messages(message)
                    landing = [home["longitude"], home["latitude"], height]
                    self.assertEqual(entry["path"][0], landing)
                    self.assertEqual(returns["landing_point"], landing)
                    self.assertEqual(len(returns["routes"]), self.area["coverage"]["total_scan_count"])
                    for part in returns["routes"]:
                        self.assertEqual(part["path"][-1], landing)
                        self.assertTrue(all(point[2] == height for point in part["path"]))
                    transit = task["transit_routes"]
                    self.assertEqual(transit["entry_path"][0], [home["longitude"], home["latitude"]])
                    self.assertEqual(len(transit["return_paths"]), self.area["coverage"]["total_scan_count"])
                    for part in transit["return_paths"]:
                        self.assertEqual(part["path"][-1], [home["longitude"], home["latitude"]])

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
            self.assertEqual(len(result["return_paths"]), self.area["coverage"]["total_scan_count"])
            self.assertEqual({part["source_uav_id"] for part in result["return_paths"]}, set(range(1, 7)))
            for path in [result["entry_path"], *(part["path"] for part in result["return_paths"])]:
                local = [[(p[1] - projection["latitude"]) * projection["north_m_per_degree"],
                          -(p[0] - projection["longitude"]) * projection["west_m_per_degree"]] for p in path]
                self.assertTrue(safe.buffer(2e-4).covers(LineString(local)))
                self.assertGreaterEqual(LineString(local).distance(self.flyable.boundary), 5.0)
        with self.assertRaisesRegex(ValueError, "不足 5 米"):
            rebase_gps_routes_for_takeoff(area, planned["planned_uavs"]["1"]["task"],
                                          {"latitude": 34.1389, "longitude": 113.9108})

    def test_cache_is_read_only_and_api_dispatch_selects_new_altitudes(self):
        from fastapi.testclient import TestClient
        from competition_backend.api import create_app
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
                default_preview = client.post("/api/v1/planning/competition-coverage", json=payload)
                self.assertEqual(default_preview.status_code, 200, default_preview.text)
                self.assertEqual(default_preview.json()["search_area"]["coverage"]["reconnaissance_radius_m"], 45.0)
                stale = client.post("/api/v1/planning/competition-coverage", json={**payload, "reconnaissance_radius_m": 75.0})
                self.assertEqual(stale.status_code, 409)
                self.assertIn("45m", stale.json()["detail"])
                payload["reconnaissance_radius_m"] = 45.0
                preview = client.post("/api/v1/planning/competition-coverage", json=payload)
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["search_area"]["gps_origin"]["latitude"], 34.138282)
                response = client.post("/api/v1/plan", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                assignments = [item["payload"] for item in app.state.adapter.snapshot() if item["type"] == "assign_task"]
                self.assertTrue(all(item["task"]["reconnaissance_radius_m"] == 45.0 for item in assignments))
                mission = response.json()["mission"]
                self.assertEqual([mission["uavs"][str(i)]["target_altitude_m"] for i in range(1, 7)],
                                 [61.0, 58.0, 55.0, 52.0, 49.0, 46.0])
                self.assertEqual([entry["payload"]["target_altitude_m"] for entry in app.state.adapter.snapshot()
                                  if entry["type"] == "assign_task"],
                                 [61.0, 58.0, 55.0, 52.0, 49.0, 46.0])
                invalid = client.post("/api/v1/planning/competition-coverage",
                                      json={**payload, "coordinate_mode": "xyz"})
                self.assertEqual(invalid.status_code, 422)
            finally:
                app.state.audit.close()

    def test_new_altitude_choice_resolves_for_every_scene(self):
        for profile in ("lab", "outdoor5", "lab10", "outdoor100", "outdoor200",
                        "competition", "dalian_nanshan", "xuchang_small"):
            self.assertEqual(_altitude_profile_for(profile, "around54m"), "around54m")
