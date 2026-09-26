import math
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from shapely.geometry import Point, Polygon, LineString, box, shape
from shapely.ops import unary_union
from competition_backend import polygon_coverage as pc

class CompetitionCompactCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan=pc.plan_competition_coverage();pc._load_geometry()

    def test_six_compact_connected_regions_cover_boundary(self):
        area=Polygon(self.plan['search_area']['points_m'])
        regions=[Polygon(u['task']['polygon_m']) for u in self.plan['planned_uavs'].values()]
        self.assertEqual(len(regions),6)
        self.assertLess(area.symmetric_difference(unary_union(regions)).area,1e-5)
        self.assertLess(sum(p.area for p in regions)-unary_union(regions).area,1e-5)
        for p in regions:
            self.assertTrue(p.is_valid)
            self.assertLessEqual(pc.shape_metrics(p)['aspect_ratio'],2.000001)
            self.assertGreaterEqual(pc.shape_metrics(p)['rectangle_fill_ratio'],.45)

    def test_discrete_scans_cover_required_area_and_flights_stay_inside(self):
        required=shape(self.plan['search_area']['terrain_layers_m']['required'])
        for runtime in self.plan['planned_uavs'].values():
            t=runtime['task'];region=Polygon(t['polygon_m']);points=t['scan_waypoints_m']
            self.assertEqual(points,t['waypoints_m'])
            self.assertEqual(t['scan_count'],len(points))
            self.assertTrue(all(region.buffer(1e-6).covers(Point(p)) for p in points))
            covered=unary_union([Point(p).buffer(74.98,quad_segs=32) for p in points])
            self.assertLess(required.intersection(region).difference(covered).area,1e-5)
            if len(t['flight_path_m'])>1:
                self.assertTrue(region.buffer(1e-6).covers(LineString(t['flight_path_m'])))
            self.assertTrue(all(a['duration_s']==10 for a in t['waypoint_actions']))

    def test_time_sum_and_parallel_time_are_different_metrics(self):
        times=[]
        for runtime in self.plan['planned_uavs'].values():
            t=runtime['task'];flight=t['flight_path_m']
            length=sum(math.dist(a,b) for a,b in zip(flight,flight[1:]))
            self.assertAlmostEqual(length,t['route_distance_m'],places=6)
            self.assertAlmostEqual(t['mission_time_s'],length/5+10*t['scan_count'],places=6)
            times.append(t['mission_time_s'])
        c=self.plan['search_area']['coverage']
        self.assertEqual(c['objective'],'minimize_total_mission_time')
        self.assertAlmostEqual(sum(times),c['total_mission_time_s'])
        self.assertAlmostEqual(max(times),c['maximum_completion_time_s'])
        self.assertGreater(sum(times),max(times))

    def test_forest_edge_and_shoreline_are_kept(self):
        area=box(0,0,1000,1000);forest=box(100,100,500,500);water=box(600,600,800,800)
        required,excluded,edge=pc.required_area(area,forest,water,75,10)
        self.assertTrue(required.covers(Point(150,300)))
        self.assertFalse(required.covers(Point(300,300)))
        self.assertTrue(required.covers(Point(605,700)))
        self.assertFalse(required.covers(Point(700,700)))
        self.assertLess(edge.difference(required).area,1e-5)
        self.assertAlmostEqual(excluded.area,250**2+180**2)
        self.assertAlmostEqual(required.area+excluded.area,area.area)

    def test_forest_buffer_is_computed_before_clipping_to_competition(self):
        area=box(0,0,100,100);forest=box(-500,-500,500,500)
        required,excluded,edge=pc.required_area(area,forest,Polygon(),75)
        self.assertTrue(required.is_empty)
        self.assertEqual(excluded.area,area.area)
        self.assertTrue(edge.is_empty)

    def test_narrow_forest_is_not_removed(self):
        area=box(0,0,300,300);forest=box(50,50,150,250)
        required,excluded,_=pc.required_area(area,forest,Polygon(),75)
        self.assertTrue(excluded.is_empty)
        self.assertEqual(required.area,area.area)

    def test_one_scan_region_and_empty_required_area(self):
        region=box(0,0,100,100)
        route=pc._hover_route(region,region,75,5,10)
        self.assertEqual(route['scan_count'],1)
        self.assertEqual(route['mission_time_s'],10)
        empty=pc._hover_route(region,Polygon(),75,5,10)
        self.assertEqual(empty['mission_time_s'],0)
        self.assertEqual(empty['waypoints_m'],[])

    def test_disable_exclusions_restores_entire_area(self):
        area=Polygon(self.plan['search_area']['points_m']);projection=self.plan['search_area']['coverage']['projection']
        required,info,_=pc._terrain(area,projection,False,75)
        self.assertTrue(required.equals(area))
        self.assertEqual(info['excluded_area_m2'],0)

    def test_cached_data_is_isolated_and_georeferenced(self):
        first=pc.plan_competition_coverage();first['planned_uavs']['1']['task']['waypoints_m'].clear()
        self.assertTrue(pc.plan_competition_coverage()['planned_uavs']['1']['task']['waypoints_m'])
        points,projection=pc.local_projection(pc.area_preset()['points'])
        for (x,y),(lat,lon) in zip(points,pc.area_preset()['points']):
            self.assertAlmostEqual(lat,projection['latitude']+x/projection['north_m_per_degree'])
            self.assertAlmostEqual(lon,projection['longitude']-y/projection['west_m_per_degree'])

    def test_invalid_parameters(self):
        for kwargs in ({'speed_mps':0},{'hover_seconds':0},{'hover_seconds':float('nan')},
                       {'max_region_aspect_ratio':1},{'forest_edge_m':-1},{'terrain_exclusions_enabled':'false'}):
            with self.assertRaises(ValueError):pc.plan_competition_coverage(**kwargs)

    def test_five_metre_edge_preserves_strip_and_excludes_deeper_forest(self):
        required,excluded,edge=pc.required_area(box(0,0,100,100),box(10,10,90,90),Polygon())
        self.assertTrue(required.covers(Point(14.9,50)))
        self.assertFalse(required.covers(Point(15.1,50)))
        self.assertAlmostEqual(excluded.area,70*70)
        self.assertLess(edge.difference(required).area,1e-7)
        self.assertEqual(self.plan['search_area']['terrain']['forest_edge_m'],5)

    def test_frozen_load_never_calls_solver_even_for_missing_or_stale_plan(self):
        with patch.object(pc,'_compute',side_effect=AssertionError('不得临场求解')):
            self.assertEqual(pc.plan_competition_coverage()['prepared_plan']['runtime_mode'],'load_prepared_only')
            for kwargs in ({'forest_edge_m':6},{'uav_count':5},{'terrain_exclusions_enabled':False}):
                with self.assertRaises(pc.PlanNotPreparedError):pc.plan_competition_coverage(**kwargs)
            with patch.object(pc,'source_digest',return_value='changed-area-or-source'):
                with self.assertRaises(pc.PlanNotPreparedError):pc.plan_competition_coverage()

    def test_corrupt_plan_is_rejected_without_running_solver(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'plan.json'
            path.write_text(json.dumps({'schema_version':1,'plan':{},'plan_sha256':'wrong'}),encoding='utf-8')
            with patch.object(pc,'_snapshot_path',return_value=path), patch.object(pc,'_compute',side_effect=AssertionError('不得临场求解')):
                with self.assertRaises(pc.PlanNotPreparedError):pc.plan_competition_coverage()

    def test_uav_count_is_not_silently_truncated(self):
        for count in (0,7,True,3.5,'6'):
            with self.assertRaises(ValueError):pc.plan_competition_coverage(uav_count=count)

if __name__=='__main__':unittest.main()
