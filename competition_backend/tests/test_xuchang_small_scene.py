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
from competition_backend.xuchang_small_scene import (
    AREA_PATH, PARTITION_REFERENCE_PATH, EAST_TRANSFER_REFERENCE_PATH,
    _partition_reference, _regions, load_xuchang_small_plan, prepare_xuchang_small_plan,
)


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


    def test_reference_limits_compact_subregions_and_complete_remainder(self):
        reference = json.loads(PARTITION_REFERENCE_PATH.read_text(encoding="utf-8"))
        previous = {uid: Polygon(points) for uid, points in reference["regions_m"].items()}
        regions = {uid: Polygon(item["task"]["polygon_m"])
                   for uid, item in self.plan["planned_uavs"].items()}
        self.assertLess(regions["6"].symmetric_difference(previous["1"]).area, 1e-5)
        self.assertGreaterEqual(regions["1"].bounds[1], previous["1"].bounds[1] - 1e-7)
        # UAV1主体沿用旧UAV3所在区；因边界浮点求交允许微小误差。
        self.assertLess(regions["1"].difference(previous["3"]).area, 1e-5)
        self.assertAlmostEqual(regions["5"].bounds[2], previous["5"].bounds[2] - 30.0, places=7)
        self.assertGreaterEqual(regions["5"].bounds[0], previous["1"].bounds[0] - 1e-7)
        self.assertGreaterEqual(regions["4"].bounds[0], regions["2"].bounds[2] - 1e-7)
        self.assertEqual(len(regions["5"].exterior.coords), 5)  # 不再保留南界的细长尾巴。
        for uid, region in regions.items():
            self.assertEqual(region.geom_type, "Polygon")
            self.assertEqual(len(region.interiors), 0)
            shape = coverage.shape_metrics(region)
            self.assertLessEqual(shape["aspect_ratio"], 3.0)
            self.assertGreaterEqual(shape["short_side_m"], 70.0)
            if uid in ("4", "2"):
                self.assertGreaterEqual(shape["rectangle_fill_ratio"], .65 if uid == "4" else .85)
            for other_uid, other in regions.items():
                if uid != other_uid:
                    self.assertLess(region.intersection(other).area, 1e-5)
        self.assertLess(unary_union(list(regions.values())).symmetric_difference(self.flyable).area, 1e-5)

    def test_reference_is_stable_and_exclusion_unchanged(self):
        reference, saved = _partition_reference(self.area["points_m"], self.area["excluded_polygons_m"][0])
        self.assertEqual(saved["excluded_polygons_m"], self.area["excluded_polygons_m"])
        self.assertEqual(saved["boundary_m"], self.area["points_m"])
        self.assertEqual(self.area["excluded_points"], [
            [34.139176470619766, 113.90981948390986],
            [34.13911894613526, 113.91127900740348],
            [34.13854369913638, 113.9112600525529],
            [34.13852278098991, 113.91014171636951],
            [34.13789000461156, 113.91004694211667],
            [34.13776449468414, 113.90978789249225]])
        cuts = self.area["coverage"]["partition_cuts_m"]
        base = json.loads(EAST_TRANSFER_REFERENCE_PATH.read_text(encoding="utf-8"))["base_plan"]
        expected = [Polygon(base["planned_uavs"][str(uid)]["task"]["polygon_m"]) for uid in range(1, 7)]
        for _ in range(2):
            actual = _regions(self.flyable, reference, cuts["north_cap_m"], cuts["west_limit_m"], cuts["west_split_m"])
            self.assertTrue(all(a.equals_exact(b, 1e-8) for a, b in zip(actual, expected)))
        bad = [list(point) for point in self.area["points_m"]]
        bad[0][0] += 1
        with self.assertRaisesRegex(ValueError, "参考与外边界或禁飞区不一致"):
            _partition_reference(bad, self.area["excluded_polygons_m"][0])

    def test_eastern_six_transfer_keeps_other_four_tasks_and_uav4_ten_points(self):
        reference = json.loads(EAST_TRANSFER_REFERENCE_PATH.read_text(encoding="utf-8"))
        base = reference["base_plan"]
        for uid in ("1", "2", "5", "6"):
            actual, old = self.plan["planned_uavs"][uid]["task"], base["planned_uavs"][{"2": "6", "6": "2"}.get(uid, uid)]["task"]
            self.assertEqual(actual["polygon_m"], old["polygon_m"])
            self.assertEqual(actual["waypoints_m"], list(reversed(old["waypoints_m"])) if uid == "1" else old["waypoints_m"])
        old4 = base["planned_uavs"]["4"]["task"]
        new4 = self.plan["planned_uavs"]["4"]["task"]
        new3 = self.plan["planned_uavs"]["3"]["task"]
        self.assertEqual(reference["transferred_waypoint_indices_1based"], [5, 6, 7, 8, 9, 10])
        self.assertEqual(new4["waypoints_m"], list(reversed(old4["waypoints_m"][:4] + old4["waypoints_m"][10:])))
        self.assertEqual(new4["scan_count"], 10)
        self.assertLess(new4["mission_time_s"], old4["mission_time_s"])
        self.assertLess(new4["route_distance_m"], old4["route_distance_m"])
        self.assertEqual(new3["scan_count"], 36)
        self.assertEqual(new3["coverage_added_scan_count"], 1)
        for point in old4["waypoints_m"][4:10]:
            self.assertIn(point, new3["waypoints_m"])
        transfer = Polygon(old4["polygon_m"]).difference(Polygon(new4["polygon_m"]))
        expected3 = Polygon(base["planned_uavs"]["3"]["task"]["polygon_m"]).union(transfer)
        self.assertLess(Polygon(new3["polygon_m"]).symmetric_difference(expected3).area, 1e-5)
        self.assertAlmostEqual(transfer.area, 15240.040305620947, places=5)
        self.assertFalse(self.area["coverage"]["eastern_transfer"]["uav4_kept_original_order"])
        self.assertTrue(self.area["coverage"]["eastern_transfer"]["uav4_kept_original_waypoint_coordinates"])

    def test_east_transfer_regeneration_never_reruns_global_partition(self):
        with patch("competition_backend.xuchang_small_scene._prepare_partition_plan",
                   side_effect=AssertionError("局部转区不能重新优化其余四机")):
            for _ in range(2):
                rebuilt = prepare_xuchang_small_plan()
                self.assertEqual(rebuilt["planned_uavs"], self.plan["planned_uavs"])
                self.assertEqual(rebuilt["search_area"], self.plan["search_area"])

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
                    "around54m": [59., 56., 53., 50., 47., 44.],
                    "subject2_50m": [50.] * 6}
        # 旧样例各机相距约1.4米，不满足新增2.5米落点避让；改为10米网格。
        projection = self.area["coverage"]["projection"]
        offsets = ((-10,10),(0,10),(10,10),(-10,0),(0,0),(10,0))
        homes = {str(uid): {"latitude": projection["latitude"] + north / projection["north_m_per_degree"],
                           "longitude": projection["longitude"] - west / projection["west_m_per_degree"]}
                 for uid, (north, west) in enumerate(offsets, 1)}
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
                                 [59.0, 56.0, 53.0, 50.0, 47.0, 44.0])
                self.assertEqual([entry["payload"]["target_altitude_m"] for entry in app.state.adapter.snapshot()
                                  if entry["type"] == "assign_task"],
                                 [59.0, 56.0, 53.0, 50.0, 47.0, 44.0])
                # 同高方案经过真实 API 校验及科目二分派，不回退到场景分层高度。
                uniform_payload = {**payload, "subject": "subject2", "flight_altitude_plan": "subject2_50m"}
                uniform_preview = client.post("/api/v1/planning/competition-coverage", json=uniform_payload)
                self.assertEqual(uniform_preview.status_code, 200, uniform_preview.text)
                uniform = client.post("/api/v1/plan", json=uniform_payload)
                self.assertEqual(uniform.status_code, 200, uniform.text)
                self.assertEqual([uniform.json()["mission"]["uavs"][str(i)]["target_altitude_m"]
                                  for i in range(1, 7)], [50.0] * 6)
                assigned = [entry["payload"] for entry in app.state.adapter.snapshot()
                            if entry["type"] == "assign_task"][-6:]
                self.assertEqual([entry["target_altitude_m"] for entry in assigned], [50.0] * 6)
                invalid = client.post("/api/v1/planning/competition-coverage",
                                      json={**payload, "coordinate_mode": "xyz"})
                self.assertEqual(invalid.status_code, 422)
            finally:
                app.state.audit.close()

    def test_new_altitude_choice_resolves_for_every_scene(self):
        from competition_backend.config import load_config
        root = Path(__file__).resolve().parents[2]
        config = load_config(str(root / "competition_backend/config/competition.example.json"))
        expected = {"around54m": [59., 56., 53., 50., 47., 44.], "subject2_50m": [50.] * 6}
        for profile in ("lab", "outdoor5", "lab10", "outdoor100", "outdoor200",
                        "competition", "dalian_nanshan", "xuchang_small"):
            for choice, heights in expected.items():
                for subject, template in config.subjects.items():
                    with self.subTest(scene=profile, altitude=choice, subject=subject):
                        selected = _altitude_profile_for(profile, choice)
                        actual = template["takeoff_altitudes_m_by_profile"][selected]
                        self.assertEqual([actual[str(uid)] for uid in range(1, 7)], heights)
