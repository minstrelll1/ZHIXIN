import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.ops import transform, unary_union

from competition_backend import clearance_plans, polygon_coverage
from competition_backend.api import create_app
from competition_backend.transit_routes import scene_plan


class ClearancePlansTest(unittest.TestCase):
    profiles = ("outdoor100", "outdoor200", "competition")
    departures = ("southeast", "stadium_center")

    @classmethod
    def setUpClass(cls):
        cls.saved = json.loads(clearance_plans.CACHE.read_text(encoding="utf-8"))["scenes"]

    def test_all_six_saved_scenes_cover_complete_required_area_with_safe_segments(self):
        self.assertEqual(set(self.saved), {p + "/" + d for p in self.profiles for d in self.departures})
        for key, saved in self.saved.items():
            with self.subTest(scene=key):
                profile, departure = key.split("/")
                raw = scene_plan(profile, departure, apply_clearance=False)
                polygon = Polygon(saved["boundary_m"])
                required = shape(saved["terrain_layers_m"]["required"])
                excluded = shape(saved["terrain_layers_m"]["excluded"])
                self.assertLess(polygon.symmetric_difference(required.union(excluded)).area, 1e-5)
                # The outer five-metre strip remains a coverage target, not a
                # removed sliver hidden by the new flight-area constraint.
                edge_target = required.difference(polygon.buffer(-5))
                self.assertGreater(edge_target.area, 1)
                disks, regions = [], []
                self.assertEqual(set(saved["tasks"]), {str(i) for i in range(1, 7)})
                for uid, task in saved["tasks"].items():
                    source = raw["planned_uavs"][uid]["task"]
                    region = Polygon(source["polygon_m"])
                    regions.append(region)
                    points = task["waypoints_m"]
                    self.assertGreater(len(points), 2)
                    self.assertGreaterEqual(len(points), len(source["waypoints_m"]))
                    self.assertEqual(task["flight_path_m"], points)
                    self.assertEqual(task["scan_count"], len(points))
                    for point in points:
                        self.assertTrue(polygon.covers(Point(point)))
                        self.assertGreaterEqual(Point(point).distance(polygon.boundary), 5)
                    for start, end in zip(points, points[1:]):
                        segment = LineString([start, end])
                        self.assertTrue(polygon.covers(segment))
                        self.assertGreaterEqual(segment.distance(polygon.boundary), 5)
                    covered = unary_union([Point(point).buffer(source["reconnaissance_radius_m"] - .02,
                                                              quad_segs=32) for point in points])
                    self.assertLess(required.intersection(region).difference(covered).area, 1e-5)
                    self.assertLess(task["uncovered_area_m2"], 1e-5)
                    disks.append(covered)
                    distance = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
                    self.assertAlmostEqual(task["route_distance_m"], distance, places=6)
                    self.assertAlmostEqual(task["mission_time_s"], distance / source["speed_mps"]
                                           + len(points) * source["hover_scan_seconds"], places=6)
                self.assertLess(polygon.symmetric_difference(unary_union(regions)).area, .01)
                self.assertLess(required.difference(unary_union(disks)).area, 1e-5)
                self.assertLess(edge_target.difference(unary_union(disks)).area, 1e-5)

    def test_terrain_matches_original_landcover_in_correct_coordinate_frame(self):
        base = polygon_coverage.plan_competition_coverage()["search_area"]
        projection = base["coverage"]["projection"]
        source_bounds = Polygon(base["points_m"]).bounds
        landcover = json.loads(Path(polygon_coverage.__file__).with_name("competition_landcover.json")
                              .read_text(encoding="utf-8"))
        def project(lon, lat, z=None):
            return ((lat - projection["latitude"]) * projection["north_m_per_degree"],
                    -(lon - projection["longitude"]) * projection["west_m_per_degree"])
        originals = {kind: unary_union([transform(project, shape(feature["geometry"]))
                                       for feature in landcover["features"]
                                       if feature["properties"].get("kind") == kind])
                     for kind in ("forest", "water")}
        for key, saved in self.saved.items():
            with self.subTest(scene=key):
                polygon = Polygon(saved["boundary_m"])
                bounds = polygon.bounds
                factor = (bounds[2] - bounds[0]) / (source_bounds[2] - source_bounds[0])
                affine = [factor, 0, 0, factor,
                          bounds[0] - factor * source_bounds[0], bounds[1] - factor * source_bounds[1]]
                forest, water = [affine_transform(originals[kind], affine) for kind in ("forest", "water")]
                for kind, expected in (("forest", forest), ("water", water)):
                    actual = shape(saved["terrain_layers_m"][kind])
                    self.assertLess(actual.symmetric_difference(expected.intersection(polygon)).area, .01)
                # Compute the old forestry/water rule on un-clipped terrain;
                # clipping before erosion incorrectly invents a forest edge.
                forest_inner = forest.buffer(-5).intersection(polygon)
                water_inner = water.buffer(-10).intersection(polygon)
                forest_edge = forest.difference(forest.buffer(-5)).intersection(polygon)
                excluded = unary_union([forest_inner, water_inner]).difference(forest_edge)
                expected = polygon.difference(excluded)
                actual = shape(saved["terrain_layers_m"]["required"])
                self.assertLess(actual.symmetric_difference(expected).area, .02)

    def test_small_and_fixed_gps_scenes_are_not_changed(self):
        for profile in ("lab", "outdoor5", "lab10", "dalian_nanshan"):
            for departure in (("fixed_dalian",) if profile == "dalian_nanshan" else self.departures):
                with self.subTest(profile=profile, departure=departure):
                    raw = scene_plan(profile, departure, apply_clearance=False)
                    before = copy.deepcopy(raw)
                    self.assertEqual(clearance_plans.apply_clearance_plan(raw), before)
                    self.assertEqual(raw, before)

    def test_runtime_uses_prepared_cache_and_preserves_original_input(self):
        with patch.object(clearance_plans, "prepare_clearance_plan",
                          side_effect=AssertionError("运行时不得重新求解覆盖规划")):
            for profile in self.profiles:
                for departure in self.departures:
                    with self.subTest(profile=profile, departure=departure):
                        raw = scene_plan(profile, departure, apply_clearance=False)
                        before = copy.deepcopy(raw)
                        changed = clearance_plans.apply_clearance_plan(raw)
                        self.assertEqual(raw, before)
                        self.assertEqual(changed["search_area"]["boundary_clearance_m"], 5)
                        self.assertTrue(changed["prepared_clearance_sha256"])
                        self.assertEqual(clearance_plans.apply_clearance_plan(changed), changed)

    def test_gps_and_xyz_use_same_cached_routes_for_multiple_references(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app({"COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": root,
                              "COMPETITION_DIAGNOSTICS_DIR": root})
            try:
                endpoint = next(r.endpoint for r in app.routes
                                if r.path == "/api/v1/planning/competition-coverage")
                for profile in self.profiles:
                    for departure in self.departures:
                        for origin in ({"latitude": 39.1, "longitude": 121.6},
                                       {"latitude": 30.78528, "longitude": 103.86102},
                                       {"latitude": -20., "longitude": -70.}):
                            with self.subTest(profile=profile, departure=departure, origin=origin):
                                common = dict(subject="subject1", flight_profile=profile,
                                              departure_point=departure, gps_origin=origin)
                                xyz = endpoint(dict(common, coordinate_mode="xyz"))
                                gps = endpoint(dict(common, coordinate_mode="gps"))
                                self.assertTrue(gps["prepared_clearance_sha256"])
                                for uid, item in gps["planned_uavs"].items():
                                    task, local = item["task"], xyz["planned_uavs"][uid]["task"]
                                    self.assertEqual(task["waypoints_m"], local["waypoints_m"])
                                    # Independently round-trip all GPS path points via
                                    # their published affine coordinate relationship.
                                    points, geo = task["waypoints_m"], task["waypoints_wgs84"]
                                    for axis in (0, 1):
                                        low = min(range(len(points)), key=lambda i: points[i][axis])
                                        high = max(range(len(points)), key=lambda i: points[i][axis])
                                        factor = ((geo[high][axis] - geo[low][axis]) /
                                                  (points[high][axis] - points[low][axis]))
                                        self.assertGreater(abs(factor), 0)
                                        for point, position in zip(points, geo):
                                            restored = points[low][axis] + (position[axis] - geo[low][axis]) / factor
                                            self.assertAlmostEqual(restored, point[axis], places=4)
            finally:
                app.state.audit.close()
