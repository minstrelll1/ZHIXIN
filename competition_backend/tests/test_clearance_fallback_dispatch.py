"""真实规划 API、分派协议、预检与程序 B 消息；测试边界仅存在内存，所有发送均模拟。"""
import copy
import json
import time
import unittest
from unittest.mock import patch

from shapely.geometry import LineString, Polygon
import test_partial_localization_dispatch as localization
from competition_backend.final_scenes import PROFILES, load_plan
from competition_backend.transit_routes import geometry_for
from su17_competition_executor.task_protocol import validate_assignment
from su17_competition_executor.transit_protocol import route_messages


class ClearanceFallbackDispatchTest(unittest.TestCase):
    setup_scene = localization.PartialLocalizationDispatchTest.setup_scene
    update = localization.PartialLocalizationDispatchTest.update
    plan = localization.PartialLocalizationDispatchTest.plan
    acknowledge = localization.PartialLocalizationDispatchTest.acknowledge

    def fixture_geometry(self, margin):
        # 仅替换内存中的测试场界：最南上层航点距界 margin 米，固定航点/坐标完全不改。
        geometry = copy.deepcopy(geometry_for(self.fixed['search_area']))
        points = [p for vehicle in geometry['vehicles'].values() for p in vehicle['waypoints_m']]
        home = self.fixed['search_area']['departure_point_m']
        x0 = min(p[0] for p in points) - margin
        x1 = max(p[0] for p in points + [home]) + 20
        y0 = min(p[1] for p in points + [home]) - 20
        y1 = max(p[1] for p in points + [home]) + 20
        geometry['boundary_m'] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        geometry['excluded_polygons_m'] = []
        return geometry

    def to_local(self, task, point):
        # 使用固定航点对应的投影，和真实 GPS 重新连接航线的转换方式一致。
        scans, gps = task['waypoints_m'], task['waypoints_wgs84']
        projection = self.fixed['search_area']['coverage']['projection']
        factors = [1 / projection['north_m_per_degree'], -1 / projection['west_m_per_degree']]
        for axis in (0, 1):
            lo, hi = min(range(len(scans)), key=lambda i: scans[i][axis]), max(range(len(scans)), key=lambda i: scans[i][axis])
            if scans[hi][axis] - scans[lo][axis] > 1e-6:
                factors[axis] = (gps[hi][axis] - gps[lo][axis]) / (scans[hi][axis] - scans[lo][axis])
        return [scans[0][0] + (point[1] - gps[0][0]) / factors[0],
                scans[0][1] + (point[0] - gps[0][1]) / factors[1]]

    def test_three_scenes_fallback_dispatch_checksums_ros_and_partial_takeoff(self):
        for profile in PROFILES:
            for excluded in ((), (2, 6)):
                with self.subTest(profile=profile, excluded=excluded):
                    self.setup_scene(profile, invalid=excluded)
                    geometry = self.fixture_geometry(4.)
                    started = time.monotonic()
                    with patch('competition_backend.transit_routes.geometry_for', return_value=geometry):
                        result = self.plan()
                    self.assertLess(time.monotonic() - started, 15.)
                    mission = result['mission']
                    expected = [i for i in range(1, 7) if i not in excluded]
                    self.assertEqual(result['dispatch_status']['assigned_uav_ids'], expected)
                    self.assertEqual(len(result['planning_warnings']), len(expected))
                    self.assertTrue(all('3米' in warning for warning in result['planning_warnings']))
                    per_uav = mission['search_area']['entry_separation']['boundary_clearance_m_by_uav']
                    self.assertEqual([per_uav[str(uid)] for uid in expected], [3.] * len(expected))
                    safe = Polygon(geometry['boundary_m']).buffer(-3., join_style=2)
                    catalog_count = sum(len(v['waypoints_m']) for v in geometry['vehicles'].values())
                    for call in self.adapter.command_assign_task.call_args_list:
                        uid, payload = call.args
                        wire = json.loads(json.dumps(dict(payload, type='assign_task', uav_id=uid), allow_nan=False))
                        checked = validate_assignment(wire, uid)
                        task = checked['task']
                        original = self.fixed['planned_uavs'][str(uid)]['task']
                        for key in ('waypoints_m', 'waypoints_wgs84', 'polygon_m', 'layers'):
                            self.assertEqual(task.get(key), original.get(key))
                        self.assertEqual(task['transit_routes']['boundary_clearance_m'], 3.)
                        self.assertEqual(mission['uavs'][str(uid)]['assignment_checksum'], checked['assignment_checksum'])
                        entry, returns = route_messages(checked)  # 与现有机载向 B 发布时相同的解析器。
                        self.assertEqual(entry['boundary_clearance_m'], 3.)
                        self.assertEqual(returns['boundary_clearance_m'], 3.)
                        self.assertEqual(len(returns['routes']), catalog_count)
                        self.assertEqual(entry['path'][0], returns['landing_point'])
                        self.assertEqual(entry['path'][-1][:2], [original['waypoints_wgs84'][0][1], original['waypoints_wgs84'][0][0]])
                        for path in [entry['path'], *(r['path'] for r in returns['routes'])]:
                            self.assertTrue(all(p[2] == checked['target_altitude_m'] for p in path))
                            self.assertTrue(safe.covers(LineString([self.to_local(task, p) for p in path])))
                        self.assertTrue(all(r['path'][-1] == returns['landing_point'] for r in returns['routes']))
                    self.assertEqual(self.fixed, load_plan(profile))
                    self.acknowledge(mission)
                    prepared = self.client.post('/api/v1/takeoff/prepare').json()
                    self.assertTrue(prepared['ready'], prepared)
                    response = self.client.post('/api/v1/takeoff/confirm', json={'token': prepared['confirmation_token']})
                    self.assertEqual(response.status_code, 200, response.text[:1000])
                    self.assertEqual([c.args[0] for c in self.adapter.command_takeoff.call_args_list], expected)

    def test_both_attempts_fail_before_any_aircraft_assignment(self):
        self.setup_scene('final_scene_2')
        with patch('competition_backend.transit_routes.geometry_for', return_value=self.fixture_geometry(2.)):
            response = self.client.post('/api/v1/plan', json=self.selection)
        self.assertEqual(response.status_code, 422, response.text[:1000])
        self.assertIn('5米和3米', response.json()['detail'])
        self.adapter.command_assign_task.assert_not_called()
        self.adapter.command_takeoff.assert_not_called()
        self.adapter.release_coordination.assert_called()

    def test_fallback_does_not_change_fixed_cache_or_next_plan(self):
        self.setup_scene('final_scene_2')
        before = self.plan()
        with patch('competition_backend.transit_routes.geometry_for', return_value=self.fixture_geometry(4.)):
            fallback = self.plan()
        after = self.plan()
        self.assertEqual(len({r['mission']['mission_id'] for r in (before, fallback, after)}), 3)
        for result, distance in ((before, 5.), (fallback, 3.), (after, 5.)):
            self.assertTrue(all(u['task']['transit_routes']['boundary_clearance_m'] == distance
                                for u in result['mission']['uavs'].values()))
        self.assertEqual(after['planning_warnings'], [])
        self.assertEqual(self.fixed, load_plan('final_scene_2'))


if __name__ == '__main__':
    unittest.main()
