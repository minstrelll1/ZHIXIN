"""许昌分机进返场通道的缓存、GPS 重接及原有 ROS 话题合同。"""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from shapely.geometry import LineString, Polygon

from competition_backend import transit_routes as transit
from competition_backend.xuchang_small_scene import load_xuchang_small_plan

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/su17_competition_executor/src'))
from su17_competition_executor.transit_protocol import route_messages


class XuchangTransitLanesIntegrationTest(unittest.TestCase):
    """仅验证规划通道保留，不将分离航线等同于全程防碰撞保证。"""

    LANE_UIDS = (1, 6, 4, 5)
    HEIGHTS = {1: 59., 2: 56., 3: 53., 4: 50., 5: 47., 6: 44.}
    HOME_OFFSETS_M = {1: (10., 0.), 2: (0., -10.), 3: (6., 8.),
                      4: (-10., 0.), 5: (0., 10.), 6: (-6., -8.)}

    @classmethod
    def setUpClass(cls):
        cls.raw = load_xuchang_small_plan()
        cls.raw_before = copy.deepcopy(cls.raw)
        cls.saved = transit.prepare_routes(cls.raw)
        cls.area = cls.raw['search_area']
        cls.flyable = Polygon(cls.area['points_m'], holes=cls.area['excluded_polygons_m'])
        cls.safe = transit.clearance_region(cls.area['points_m'], 5.,
                                             holes=cls.area['excluded_polygons_m'])
        cls.expected_keys = [(int(uid), index + 1)
                             for uid, wrapper in sorted(cls.raw['planned_uavs'].items(), key=lambda pair: int(pair[0]))
                             for index in range(len(wrapper['task']['waypoints_m']))]
        cls.original_cache = json.loads(transit.CACHE.read_text(encoding='utf-8'))
        cache = copy.deepcopy(cls.original_cache)
        cache['scenes']['xuchang_small/fixed_xuchang'] = cls.saved
        canonical = json.dumps(cache['scenes'], sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        cache['sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        path = Path(cls.temporary.name) / 'transit.json'
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
        cls.cache_patch = patch.object(transit, 'CACHE', path)
        cls.cache_patch.start()
        cls.addClassCleanup(cls.cache_patch.stop)
        cls.plan = transit.attach_routes(copy.deepcopy(cls.raw))

    def local(self, point):
        projection = self.area['coverage']['projection']
        return [(point[1] - projection['latitude']) * projection['north_m_per_degree'],
                -(point[0] - projection['longitude']) * projection['west_m_per_degree']]

    def gps_home(self, uid):
        projection = self.area['coverage']['projection']
        north, west = self.HOME_OFFSETS_M[uid]
        return dict(latitude=projection['latitude'] + north / projection['north_m_per_degree'],
                    longitude=projection['longitude'] - west / projection['west_m_per_degree'])

    def assert_local_path(self, actual_gps, expected_m):
        self.assertEqual(len(actual_gps), len(expected_m))
        for actual, expected in zip(actual_gps, expected_m):
            self.assertLess(math.dist(self.local(actual), expected), 2e-4)

    def test_prepare_keeps_scan_geometry_and_per_recipient_lane_catalog(self):
        self.assertEqual(self.raw, self.raw_before)
        for uid, wrapper in self.raw['planned_uavs'].items():
            vehicle = self.saved['vehicles'][str(uid)]
            self.assertEqual(vehicle['waypoints_m'], wrapper['task']['waypoints_m'])
            if int(uid) not in self.LANE_UIDS:
                self.assertNotIn('transit_lane', vehicle)
                continue
            lane = vehicle['transit_lane']
            self.assertEqual(vehicle['entry_path_m'][0], self.saved['departure_m'])
            self.assertEqual(vehicle['entry_path_m'][1], lane['entry_gate_m'])
            self.assertEqual(vehicle['entry_path_m'][-1], vehicle['waypoints_m'][0])
            parts = vehicle['fleet_return_paths_m']
            self.assertEqual([(r['source_uav_id'], r['waypoint_index']) for r in parts], self.expected_keys)
            for part in parts:
                source = self.saved['vehicles'][str(part['source_uav_id'])]
                self.assertEqual(part['path'][0], source['waypoints_m'][part['waypoint_index'] - 1])
                self.assertEqual(part['path'][-2], lane['return_gate_m'])
                self.assertEqual(part['path'][-1], self.saved['departure_m'])
            for route in [vehicle['entry_path_m'], *(r['path'] for r in parts)]:
                line = LineString(route)
                self.assertTrue(self.safe.buffer(1e-7).covers(line))
                self.assertGreaterEqual(line.distance(self.flyable.boundary), 5.)
        for gate in ('entry_gate_m', 'return_gate_m'):
            gates = [tuple(self.saved['vehicles'][str(uid)]['transit_lane'][gate]) for uid in self.LANE_UIDS]
            self.assertEqual(len(set(gates)), len(self.LANE_UIDS))

    def test_attach_loads_recipient_routes_instead_of_one_shared_fleet_route(self):
        for uid in self.LANE_UIDS:
            vehicle = self.saved['vehicles'][str(uid)]
            routes = self.plan['planned_uavs'][str(uid)]['task']['transit_routes']
            self.assertEqual(routes['schema_version'], 2)
            self.assertEqual(routes['recipient_uav_id'], uid)
            self.assertEqual(routes['route_scope'], 'all_uav_waypoints')
            self.assert_local_path(routes['entry_path'], vehicle['entry_path_m'])
            self.assertEqual([(r['source_uav_id'], r['waypoint_index']) for r in routes['return_paths']], self.expected_keys)
            for actual, expected in zip(routes['return_paths'], vehicle['fleet_return_paths_m']):
                self.assert_local_path(actual['path'], expected['path'])

    def test_ten_meter_home_rebase_preserves_fixed_lane_interior_for_all_sources(self):
        for uid in self.LANE_UIDS:
            with self.subTest(uav=uid):
                task = self.plan['planned_uavs'][str(uid)]['task']
                before = copy.deepcopy(task)
                vehicle = self.saved['vehicles'][str(uid)]
                home = self.gps_home(uid)
                routes = transit.rebase_gps_routes_for_takeoff(self.area, task, home)
                self.assertEqual(task, before)
                self.assertEqual(routes['departure'], [home['longitude'], home['latitude']])
                self.assertEqual(routes['entry_path'][0], routes['departure'])
                fixed_entry = vehicle['entry_path_m'][1:]
                self.assert_local_path(routes['entry_path'][-len(fixed_entry):], fixed_entry)
                self.assertEqual([(r['source_uav_id'], r['waypoint_index']) for r in routes['return_paths']], self.expected_keys)
                for actual, fixed in zip(routes['return_paths'], vehicle['fleet_return_paths_m']):
                    fixed_interior = fixed['path'][:-1]
                    self.assert_local_path(actual['path'][:len(fixed_interior)], fixed_interior)
                    self.assertEqual(actual['path'][-1], routes['departure'])
                for path in [routes['entry_path'], *(r['path'] for r in routes['return_paths'])]:
                    local_line = LineString([self.local(point) for point in path])
                    self.assertTrue(self.safe.buffer(2e-4).covers(local_line))
                    self.assertGreaterEqual(local_line.distance(self.flyable.boundary), 5.)

    def test_protocol_all_six_sources_land_at_receiver_home_at_receiver_altitude(self):
        for uid in range(1, 7):
            with self.subTest(uav=uid):
                task = copy.deepcopy(self.plan['planned_uavs'][str(uid)]['task'])
                home = self.gps_home(uid)
                task['transit_routes'] = transit.rebase_gps_routes_for_takeoff(self.area, task, home)
                altitude = self.HEIGHTS[uid]
                assignment = dict(task=task, uav_id=uid, mission_id='subject1-lane-contract',
                                  assignment_checksum='contract', coordinate_frame='WGS84',
                                  target_altitude_m=altitude,
                                  landing_point_wgs84=[home['latitude'], home['longitude'], altitude])
                entry, returns = route_messages(assignment)
                landing = [home['longitude'], home['latitude'], altitude]
                self.assertEqual(entry['schema_version'], 1)
                self.assertEqual(returns['schema_version'], 2)
                self.assertEqual(entry['coordinate_order'], 'longitude_latitude_relative_altitude')
                self.assertEqual(returns['route_scope'], 'all_uav_waypoints')
                self.assertEqual(returns['landing_point'], landing)
                self.assertEqual(entry['path'][0], landing)
                self.assertEqual([(r['source_uav_id'], r['waypoint_index']) for r in returns['routes']], self.expected_keys)
                for part in returns['routes']:
                    source = self.raw['planned_uavs'][str(part['source_uav_id'])]['task']
                    lat, lon = source['waypoints_wgs84'][part['waypoint_index'] - 1][:2]
                    self.assertEqual(part['path'][0], [lon, lat, altitude])
                    self.assertEqual(part['path'][-1], landing)
                    self.assertTrue(all(point[2] == altitude for point in part['path']))
                self.assertTrue(all(point[2] == altitude for point in entry['path']))
                self.assertNotIn('transit_lane', entry)
                self.assertNotIn('fleet_return_paths_m', returns)

    def test_non_xuchang_preparation_still_matches_original_fixed_cache(self):
        for profile, departure in (('lab', 'southeast'), ('outdoor5', 'stadium_center'),
                                   ('dalian_nanshan', 'fixed_dalian')):
            with self.subTest(profile=profile):
                plan = transit.scene_plan(profile, departure)
                generated = transit.prepare_routes(plan)
                existing = self.original_cache['scenes'][profile + '/' + departure]
                self.assertEqual(generated, existing)
                self.assertTrue(all('transit_lane' not in vehicle for vehicle in generated['vehicles'].values()))


if __name__ == '__main__':
    unittest.main()
