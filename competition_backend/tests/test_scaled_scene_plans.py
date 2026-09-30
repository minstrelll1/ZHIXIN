import math
import unittest
from unittest.mock import patch

from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import unary_union

from competition_backend import polygon_coverage, stadium_departure
from competition_backend.scaled_scene_plans import load_scaled_scene_plan


class ScaledScenePlansTest(unittest.TestCase):
    def test_four_prepared_scenes_cover_required_area_within_boundary(self):
        base = polygon_coverage.plan_competition_coverage()
        with patch("competition_backend.scaled_scene_plans.prepare_scaled_scene_plan",
                   side_effect=AssertionError("运行时不得重新规划")):
            for profile, extent in (("outdoor100", 100), ("outdoor200", 200)):
                for departure in ("southeast", "stadium_center"):
                    with self.subTest(profile=profile, departure=departure):
                        source = (base if departure == "southeast" else
                                  stadium_departure.load_prepared_stadium_plan(
                                      base, flight_profile="competition"))
                        source_area = source["search_area"]
                        scale = extent / max(source_area["width_m"], source_area["height_m"])
                        plan = load_scaled_scene_plan(
                            base, flight_profile=profile, departure_point=departure,
                        )
                        area = plan["search_area"]
                        boundary = Polygon(area["points_m"])
                        self.assertTrue(boundary.is_valid)
                        self.assertLessEqual(max(area["width_m"], area["height_m"]), extent + 1e-5)
                        self.assertEqual(len(plan["planned_uavs"]), 6)
                        self.assertEqual(
                            area["coverage"]["total_scan_count"],
                            73 if departure == "southeast" else 75,
                        )
                        self.assertAlmostEqual(area["coverage"]["uncovered_area_m2"], 0.0, places=5)
                        self.assertEqual(area["coverage"]["reconnaissance_radius_m"], 75.0)
                        self.assertEqual(area["coverage"]["speed_mps"], 5.0)
                        self.assertEqual(area["departure_point_m"], [0.0, 0.0])
                        required = shape(area["terrain_layers_m"]["required"])
                        regions = []
                        for uav_id, wrapper in plan["planned_uavs"].items():
                            task = wrapper["task"]
                            source_scans = source["planned_uavs"][uav_id]["task"]["waypoints_m"]
                            self.assertEqual(task["scan_count"], len(source_scans))
                            # The complete original order and route shape are
                            # scaled; no waypoint is dropped by a new solver.
                            for source_point, scaled_point in zip(source_scans, task["waypoints_m"]):
                                for axis in (0, 1):
                                    self.assertAlmostEqual(
                                        scaled_point[axis] - task["waypoints_m"][0][axis],
                                        (source_point[axis] - source_scans[0][axis]) * scale,
                                        places=5,
                                    )
                            self.assertTrue(math.isfinite(task["mission_time_s"]))
                            self.assertEqual(task["scan_count"], len(task["waypoints_m"]))
                            region = Polygon(task["polygon_m"])
                            regions.append(region)
                            self.assertTrue(boundary.buffer(1e-4).covers(region))
                            for waypoint in task["waypoints_m"]:
                                self.assertTrue(region.buffer(1e-4).covers(Point(waypoint)))
                            covered = unary_union([
                                Point(point).buffer(74.98, quad_segs=32)
                                for point in task["scan_waypoints_m"]
                            ])
                            self.assertLess(required.intersection(region).difference(covered).area, 1e-5)
                            flight = task["flight_path_m"]
                            if task["scan_count"]:
                                self.assertEqual(flight[0], [0.0, 0.0])
                                self.assertEqual(flight[-1], [0.0, 0.0])
                            else:
                                self.assertEqual(flight, [])
                            distance = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
                            self.assertAlmostEqual(distance, task["route_distance_m"], places=5)
                            self.assertAlmostEqual(
                                task["mission_time_s"], distance / 5.0 + task["scan_count"] * 10.0,
                                places=5,
                            )
                            for line in task["route_primitives"]:
                                self.assertTrue(boundary.buffer(1e-4).covers(
                                    LineString([line["start"], line["end"]])))
                        merged = unary_union(regions)
                        self.assertLess(boundary.symmetric_difference(merged).area, 1e-3)
                        self.assertLess(sum(region.area for region in regions) - merged.area, 1e-5)

    def test_gps_points_use_requested_reference_without_changing_cached_xyz(self):
        base = polygon_coverage.plan_competition_coverage()
        origin = {"latitude": 30.78528, "longitude": 103.86102}
        for departure in ("southeast", "stadium_center"):
            with self.subTest(departure=departure):
                gps = load_scaled_scene_plan(
                    base, flight_profile="outdoor200", departure_point=departure,
                    coordinate_mode="gps", gps_origin=origin,
                )
                self.assertEqual(gps["search_area"]["coordinate_mode"], "gps")
                self.assertEqual(len(gps["search_area"]["points"]), len(gps["search_area"]["points_m"]))
                for item in gps["planned_uavs"].values():
                    task = item["task"]
                    self.assertEqual(task["coordinate_frame"], "LOCAL_NORTH_WEST")
                    self.assertEqual(len(task["waypoints_wgs84"]), task["scan_count"])
                    for lat, lon in task["waypoints_wgs84"]:
                        self.assertLess(abs(lat - origin["latitude"]), 0.003)
                        self.assertLess(abs(lon - origin["longitude"]), 0.003)
                xyz = load_scaled_scene_plan(
                    base, flight_profile="outdoor200", departure_point=departure,
                )
                self.assertEqual(xyz["search_area"]["coordinate_mode"], "xyz")
                self.assertFalse(xyz["planned_uavs"]["1"]["task"].get("waypoints_wgs84"))


if __name__ == "__main__":
    unittest.main()
