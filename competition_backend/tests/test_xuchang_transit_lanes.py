"""许昌四机进返场方向：固定几何分流、原任务保持和安全边距。"""
import copy
import itertools
import unittest
from shapely.geometry import LineString, Point, Polygon
from competition_backend.xuchang_small_scene import _prepare_east_transfer_plan, load_xuchang_small_plan
from competition_backend.xuchang_transit_lanes import LANES, build_recipient_routes


class XuchangTransitLanesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_xuchang_small_plan()
        cls.base = _prepare_east_transfer_plan()
        cls.area = cls.plan["search_area"]
        cls.flyable = Polygon(cls.area["points_m"], holes=cls.area["excluded_polygons_m"])
        cls.points = {uid: item["task"]["waypoints_m"] for uid, item in cls.plan["planned_uavs"].items()}
        cls.routes = {uid: build_recipient_routes(cls.area["points_m"], cls.area["excluded_polygons_m"],
                                                [0., 0.], cls.points, uid) for uid in LANES}

    def test_only_one_and_four_scan_order_reversed_regions_and_parameters_unchanged(self):
        for uid in range(1, 7):
            old, new = (p["planned_uavs"][str(uid)]["task"] for p in (self.base, self.plan))
            for key in ("polygon_m", "area_m2", "bounds_m", "speed_mps", "hover_scan_seconds", "reconnaissance_radius_m", "scan_count"):
                self.assertEqual(old[key], new[key], (uid, key))
            for key in ("waypoints_m", "scan_waypoints_m", "waypoints_wgs84"):
                self.assertEqual(new[key], list(reversed(old[key])) if uid in (1, 4) else old[key])
            if uid in (3, 6):
                self.assertEqual(old, new)
                self.assertIsNone(build_recipient_routes(self.area["points_m"], self.area["excluded_polygons_m"],
                                                        [0., 0.], self.points, uid))

    def test_explicit_home_gates_and_all_fleet_points_safe_return_paths(self):
        total = sum(len(points) for points in self.points.values())
        expected = {(int(uid), index + 1) for uid, points in self.points.items() for index in range(len(points))}
        for uid, routes in self.routes.items():
            lane = routes["transit_lane"]
            self.assertEqual(routes["entry_path_m"][0], [0., 0.])
            self.assertEqual(routes["entry_path_m"][1], lane["entry_gate_m"])
            self.assertEqual(routes["entry_path_m"][-1], self.points[str(uid)][0])
            self.assertEqual(len(routes["fleet_return_paths_m"]), total)
            self.assertEqual({(r["source_uav_id"], r["waypoint_index"]) for r in routes["fleet_return_paths_m"]}, expected)
            for part in routes["fleet_return_paths_m"]:
                self.assertEqual(part["path"][0], self.points[str(part["source_uav_id"])][part["waypoint_index"] - 1])
                self.assertEqual(part["path"][-2], lane["return_gate_m"])
                self.assertEqual(part["path"][-1], [0., 0.])
            for path in [routes["entry_path_m"], *[part["path"] for part in routes["fleet_return_paths_m"]]]:
                line = LineString(path)
                self.assertTrue(self.flyable.buffer(1e-7).covers(line))
                self.assertGreaterEqual(line.distance(self.flyable.boundary), 5.0)

    def test_first_and_second_entry_no_longer_share_southwestern_corridor(self):
        one = self.routes[1]["entry_path_m"]
        two = self.routes[2]["entry_path_m"]
        self.assertGreater(max(point[0] for point in one), 100)
        self.assertLess(min(point[0] for point in two), -80)
        self.assertLess(LineString(one).intersection(LineString(two)).length, 1e-7)
        old_shared_corner = [-66.23552249897692, -60.38387146290231]
        self.assertGreater(LineString(one).distance(Point(old_shared_corner)), 70)

    def test_entry_and_final_return_are_separated_outside_twenty_meter_home_zone(self):
        zone = Point(0, 0).buffer(20, quad_segs=256)
        selected = {}
        for uid, routes in self.routes.items():
            final = next(r["path"] for r in routes["fleet_return_paths_m"]
                         if r["source_uav_id"] == uid and r["waypoint_index"] == len(self.points[str(uid)]))
            selected[uid] = [LineString(p).difference(zone) for p in (routes["entry_path_m"], final)]
        for uid, paths in selected.items():
            for other, other_paths in selected.items():
                if uid >= other:
                    continue
                distance = min(a.distance(b) for a in paths for b in other_paths)
                self.assertGreater(distance, 13.0, (uid, other, distance))
                if (uid, other) == (1, 2):
                    self.assertGreater(distance, 19.0)
        # 同一名义起点在20m近场内仍会汇合，本验证不是全时段防碰撞保证。
        self.assertEqual(LineString(self.routes[1]["entry_path_m"]).distance(LineString(self.routes[2]["entry_path_m"])), 0.)
        self.assertFalse(self.area["coverage"]["dedicated_transit_lanes"]["collision_avoidance_guaranteed"])

    def test_ten_meter_square_parking_layouts_keep_approach_lanes_separated(self):
        # 代表性布局压力检查：10m见方四角、24种机号排列；不外推为任意实GPS保证。
        zone = Point(0, 0).buffer(20, quad_segs=128)
        ids = [1, 2, 4, 5]
        corners = [[5., -5.], [-5., -5.], [5., 5.], [-5., 5.]]
        for homes in itertools.permutations(corners):
            paths = {}
            for uid, home in zip(ids, homes):
                routes = self.routes[uid]
                returning = next(part["path"] for part in routes["fleet_return_paths_m"]
                                 if part["source_uav_id"] == uid and part["waypoint_index"] == len(self.points[str(uid)]))
                entry = [home, *routes["entry_path_m"][1:]]
                returning = [*returning[:-1], home]
                for line in (LineString(entry), LineString(returning)):
                    self.assertTrue(self.flyable.buffer(1e-7).covers(line))
                    self.assertGreaterEqual(line.distance(self.flyable.boundary), 5.)
                paths[uid] = [LineString(entry).difference(zone), LineString(returning).difference(zone)]
            for uid, other in itertools.combinations(ids, 2):
                separation = min(a.distance(b) for a in paths[uid] for b in paths[other])
                self.assertGreater(separation, 11., (uid, other, homes, separation))

    def test_flight_path_and_estimates_include_the_same_lane_geometry(self):
        for uid, routes in self.routes.items():
            task = self.plan["planned_uavs"][str(uid)]["task"]
            final = next(r["path"] for r in routes["fleet_return_paths_m"]
                         if r["source_uav_id"] == uid and r["waypoint_index"] == len(task["waypoints_m"]))
            expected = [*routes["entry_path_m"], *task["waypoints_m"][1:], *final[1:]]
            self.assertEqual(task["flight_path_m"], expected)
            self.assertAlmostEqual(task["route_distance_m"], LineString(expected).length, places=6)
            self.assertAlmostEqual(task["mission_time_s"], task["route_distance_m"] / task["speed_mps"] + task["scan_count"] * task["hover_scan_seconds"])


if __name__ == "__main__":
    unittest.main()
