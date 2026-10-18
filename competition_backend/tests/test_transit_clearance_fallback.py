"""检查实际几何和回退边界；不连接网络或无人机。"""
import unittest
from unittest.mock import patch

from shapely.geometry import LineString, Point, Polygon
from competition_backend import transit_preferences as preferences
from competition_backend.transit_routes import ClearanceRouteError


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
HOLE = [[40, 40], [60, 40], [60, 60], [40, 60]]


def neck(width):
    low, high = 15 - width / 2, 15 + width / 2
    return [[0, 0], [30, 0], [30, low], [60, low], [60, 0], [90, 0],
            [90, 30], [60, 30], [60, high], [30, high], [30, 30], [0, 30]]


class ClearanceFallbackTest(unittest.TestCase):
    def plan(self, boundary=SQUARE, home=(20, 20), targets=((80, 80),), others=(), holes=()):
        with patch.object(preferences, '_routes_at_clearance', wraps=preferences._routes_at_clearance) as attempt:
            result = preferences.routes_from_home(boundary, home, targets, others, holes=holes)
        return *result, [c.kwargs['clearance'] for c in attempt.call_args_list]

    def assert_routes(self, paths, boundary, holes, clearance, home, targets):
        region = Polygon(boundary, holes=holes)
        safe = region.buffer(-clearance, join_style=2).buffer(1e-7)
        for path, target in zip(paths, targets):
            self.assertEqual(path[0], list(home))
            self.assertEqual(path[-1], list(target))
            # 起降连接段可以穿过边缘带，其他完整航段必须符合实际采用的间距。
            interior = path if safe.covers(Point(home)) else path[1:]
            line = LineString(interior) if len(interior) > 1 else Point(interior[0])
            self.assertTrue(safe.covers(line), (clearance, path))
            for hole in holes:
                self.assertFalse(LineString(path).relate_pattern(Polygon(hole), 'T********'))

    def test_five_is_kept_when_available_even_if_soft_home_avoidance_fails(self):
        for others in ([], [[20, 20]]):
            paths, info, attempts = self.plan(others=others)
            self.assertEqual(attempts, [5.])
            self.assertEqual(info['boundary_clearance_m'], 5.)
            self.assertFalse(info['boundary_clearance_relaxed'])
            self.assert_routes(paths, SQUARE, [], 5., [20, 20], [[80, 80]])

    def test_target_four_metres_from_boundary_uses_three_with_home_connectors(self):
        for home in ([20, 20], [2, 20], [-10, 20]):
            with self.subTest(home=home):
                paths, info, attempts = self.plan(home=home, targets=[[4, 80]])
                self.assertEqual(attempts, [5., 3.])
                self.assertEqual(info['boundary_clearance_m'], 3.)
                self.assertTrue(info['boundary_clearance_relaxed'])
                self.assertTrue(info['boundary_clearance_fallback_reason'])
                self.assert_routes(paths, SQUARE, [], 3., home, [[4, 80]])

    def test_narrow_corridor_becomes_connected_at_three(self):
        for home in ([15, 15], [-10, 15]):
            paths, info, attempts = self.plan(boundary=neck(8), home=home, targets=[[75, 15]])
            self.assertEqual(attempts, [5., 3.])
            self.assert_routes(paths, neck(8), [], 3., home, [[75, 15]])

    def test_hole_is_preserved_when_three_is_used(self):
        for home in ([20, 50], [-10, 50]):
            targets = [[64, 50], [80, 80]]
            paths, info, attempts = self.plan(home=home, targets=targets, holes=[HOLE], others=[[64, 50]])
            self.assertEqual(attempts, [5., 3.])
            self.assertEqual(info['boundary_clearance_m'], 3.)
            self.assertTrue(info['home_avoidance_relaxed'])
            self.assert_routes(paths, SQUARE, [HOLE], 3., home, targets)

    def test_three_is_a_floor_and_does_not_fall_back_to_zero(self):
        for boundary, targets, holes in ((SQUARE, [[2, 80]], []),
                                          (neck(5), [[75, 15]], []),
                                          (SQUARE, [[62, 50]], [HOLE])):
            with self.subTest(targets=targets):
                with patch.object(preferences, '_routes_at_clearance', wraps=preferences._routes_at_clearance) as attempt:
                    with self.assertRaisesRegex(ClearanceRouteError, '5米和3米'):
                        preferences.routes_from_home(boundary, [15, 15], targets, [], holes=holes)
                self.assertEqual([c.kwargs['clearance'] for c in attempt.call_args_list], [5., 3.])

    def test_invalid_geometry_and_excluded_points_do_not_trigger_retry(self):
        cases = [(SQUARE, [50, 50], [[80, 80]], [HOLE]),
                 (SQUARE, [20, 20], [[50, 50]], [HOLE]),
                 (SQUARE, [20, 20], [[101, 80]], []),
                 ([[0, 0], [100, 100], [0, 100], [100, 0]], [20, 20], [[80, 80]], [])]
        for boundary, home, targets, holes in cases:
            with self.subTest(home=home, targets=targets):
                with patch.object(preferences, '_routes_at_clearance') as attempt:
                    with self.assertRaises(ValueError):
                        preferences.routes_from_home(boundary, home, targets, [], holes=holes)
                attempt.assert_not_called()

    def test_unrelated_errors_are_not_retried_or_hidden(self):
        with patch.object(preferences, '_routes_at_clearance', side_effect=ValueError('坏坐标')) as attempt:
            with self.assertRaisesRegex(ValueError, '坏坐标'):
                preferences.routes_from_home(SQUARE, [20, 20], [[80, 80]], [])
        self.assertEqual(attempt.call_count, 1)

    def test_entry_optimization_respects_each_aircraft_clearance(self):
        entries = {1: [[20, 20], [80, 80]], 2: [[20, 20], [4, 80]], 3: [[80, 20], [20, 80]]}
        homes = {uid: route[0] for uid, route in entries.items()}
        limits = {1: 5., 2: 3., 3: 5.}
        paths, _ = preferences.prefer_entries(SQUARE, entries, homes, clearance_by_uav=limits)
        for uid, path in paths.items():
            self.assert_routes([path], SQUARE, [], limits[uid], homes[uid], [entries[uid][-1]])

    def test_non_five_modes_keep_their_existing_single_attempt(self):
        for distance in (0., 3.):
            with patch.object(preferences, '_routes_at_clearance', wraps=preferences._routes_at_clearance) as attempt:
                preferences.routes_from_home(SQUARE, [20, 20], [[80, 80]], [], clearance=distance)
            self.assertEqual([c.kwargs['clearance'] for c in attempt.call_args_list], [distance])

    def test_clearance_thresholds_and_translated_coordinates(self):
        # 保留2cm舍入余量；不同局部原点下5/3m结果及实际几何均一致。
        for dx, dy in ((0, 0), (1000, -2000), (-500, 700)):
            shift = lambda p: [p[0] + dx, p[1] + dy]
            boundary = [shift(p) for p in SQUARE]
            for margin, expected in ((2.9, None), (3.01, None), (3.03, 3.), (4., 3.), (5.01, 3.), (5.03, 5.)):
                with self.subTest(origin=(dx, dy), margin=margin):
                    home, targets = shift([20, 20]), [shift([margin, 80])]
                    if expected is None:
                        with self.assertRaisesRegex(ClearanceRouteError, '5米和3米'):
                            self.plan(boundary=boundary, home=home, targets=targets)
                        continue
                    paths, info, attempts = self.plan(boundary=boundary, home=home, targets=targets)
                    self.assertEqual(info['boundary_clearance_m'], expected)
                    self.assertEqual(attempts, [5.] if expected == 5. else [5., 3.])
                    self.assert_routes(paths, boundary, [], expected, home, targets)


if __name__ == '__main__':
    unittest.main()
