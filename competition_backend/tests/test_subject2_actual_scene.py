import copy
import itertools
import math
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from shapely.geometry import Polygon, Point, LineString, shape
from shapely.ops import unary_union

from competition_backend.api import create_app
from competition_backend.subject2_actual_scene import load_plan, PROFILE, BUILDINGS, MAX_MISSION_SECONDS, UAV35_REGION_WGS84, UAV3_OPTIONAL_WGS84, local_point, update_timing
from competition_backend.subject1_actual_scene import load_plan as subject1_plan
from competition_backend.transit_routes import attach_routes
from competition_backend.subject2_entry_routes import validate_entries
from su17_competition_executor.task_protocol import validate_assignment
from su17_competition_executor.transit_protocol import route_messages


def homes_for(plan):
    a=plan['search_area']; p=a['coverage']['projection']; d=a['departure_point_m']
    return {str(uid):dict(latitude=p['latitude']+(d[0]+(3.5-uid)*8)/p['north_m_per_degree'],
        longitude=p['longitude']-d[1]/p['west_m_per_degree'],boot_id='test-'+str(uid),
        source='first_valid_unarmed_gps_per_boot') for uid in range(1,7)}


class Subject2ActualTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan=attach_routes(load_plan())

    def test_entry_validation_rejects_crossings_inside_launch_fan(self):
        boundary=[[0,0],[100,0],[100,100],[0,100]]
        homes={1:[35,40],2:[35,60]}
        entries={1:[[35,40],[65,60]],2:[[35,60],[65,40]]}
        with self.assertRaisesRegex(ValueError,'交叉'):
            validate_entries(boundary,homes,entries,[50,50])

    def test_geometry_time_coverage_buildings_and_scan_exclusions(self):
        p=self.plan; a=p['search_area']; boundary=Polygon(a['points_m'])
        self.assertEqual(a['points'],subject1_plan()['search_area']['points'])
        self.assertEqual(a['departure_point'],'stadium_center')
        self.assertEqual(a['coordinate_mode'],'gps')
        self.assertTrue(a['terrain']['enabled'])
        exclusions=unary_union([Polygon(r) for r in a['scan_exclusion_polygons_m']])
        self.assertEqual(a['scan_exclusion_point_ids'],[[2,3,4,5],[1,10,11,25]])
        for ring,ids in zip(a['scan_exclusion_polygons_m'],a['scan_exclusion_point_ids']):
            self.assertEqual(ring,[a['points_m'][i-1] for i in ids])
        required=shape(a['terrain_layers_m']['required'])
        regions=[];disks=[];entries={}
        for uid,item in p['planned_uavs'].items():
            t=item['task']; region=Polygon(t['polygon_m']);regions.append(region)
            points=t['waypoints_m'];radius=t['reconnaissance_radius_m']
            self.assertLessEqual(len(points),16)
            self.assertEqual(t['hover_scan_seconds'],30.)
            self.assertEqual(t['scan_passes'],2)
            self.assertEqual(t['scan_visit_count'],2*len(points))
            self.assertEqual(t['scan_execution_order'],list(range(len(points)))+list(reversed(range(len(points)))))
            self.assertTrue(all(action['duration_s']==30. for action in t['waypoint_actions']))
            for point in points:
                self.assertTrue(region.contains(Point(point)))
                self.assertFalse(shape(a['terrain_layers_m']['excluded']).contains(Point(point)))
                self.assertGreaterEqual(exclusions.distance(Point(point)),radius)
                disks.append(Point(point).buffer(radius-.02,quad_segs=64))
            line=LineString(t['flight_path_m'])
            self.assertTrue(boundary.covers(line))
            self.assertGreaterEqual(line.distance(boundary.boundary),5.)
            expected=line.length/5+len(points)*60+t['coverage_reference_altitude_m']/6
            self.assertAlmostEqual(t['mission_time_s'],expected,places=6)
            self.assertLessEqual(expected,MAX_MISSION_SECONDS)
            entries[int(uid)]=t['entry_path_m']
        for first, second in itertools.combinations(regions, 2):
            self.assertLess(first.intersection(second).area, 1e-6)
        for uid, item in p['planned_uavs'].items():
            for other, region in enumerate(regions, 1):
                if other != int(uid):
                    self.assertTrue(all(not region.contains(Point(q)) for q in item['task']['waypoints_m']))
        common=Polygon([local_point(a['coverage']['projection'],lat,lon) for lat,lon in UAV35_REGION_WGS84]).intersection(boundary)
        focus=Polygon(a['priority_regions'][0]['polygon_m'])
        task3=p['planned_uavs']['3']['task']
        western=[Polygon(p['planned_uavs'][uid]['task']['polygon_m']) for uid in ('3','5')]
        self.assertLess(common.symmetric_difference(unary_union(western).intersection(common)).area,1e-6)
        self.assertTrue(common.buffer(1e-7).covers(western[1]))
        self.assertTrue(common.buffer(1e-7).covers(LineString(p['planned_uavs']['5']['task']['waypoints_m'])))
        self.assertEqual(len(p['planned_uavs']['5']['task']['waypoints_m']),6)
        self.assertTrue(common.covers(LineString(task3['waypoints_m'][:5])))
        self.assertEqual(sum(common.covers(Point(q)) for q in task3['waypoints_m']),5)
        self.assertEqual(task3['waypoints_m'][0][0],min(q[0] for q in task3['waypoints_m']))
        optional=a['western_rebalance']
        self.assertTrue(optional['subregions_disjoint'])
        self.assertTrue(optional['shared_boundaries_allowed'])
        self.assertLess(optional['maximum_subregion_overlap_m2'],1e-6)
        self.assertTrue(optional['optional_included'])
        self.assertEqual(len(task3['waypoints_m']),6)
        self.assertEqual(task3['waypoints_m'][-1],optional['optional_point_m'])
        requested=local_point(a['coverage']['projection'],*UAV3_OPTIONAL_WGS84)
        self.assertLess(math.dist(requested,task3['waypoints_m'][-1]),53.)
        original=subject1_plan()
        for uid in ('1','2','4','6'):
            self.assertEqual(p['planned_uavs'][uid]['task']['polygon_m'],original['planned_uavs'][uid]['task']['polygon_m'])
        own_disks=unary_union([Point(q).buffer(55-.02,quad_segs=64) for q in task3['waypoints_m']])
        focused_required=focus.intersection(required)
        self.assertGreater(own_disks.intersection(focused_required).area/focused_required.area,.90)
        for i,point in enumerate(task3['waypoints_m']):
            for other in task3['waypoints_m'][i+1:]:self.assertGreaterEqual(math.dist(point,other),55.)
        self.assertAlmostEqual(a['priority_regions'][0]['coverage_ratio'],own_disks.intersection(focused_required).area/focused_required.area)
        self.assertAlmostEqual(unary_union(disks).intersection(required).area/required.area,a['coverage']['coverage_ratio'],places=7)
        self.assertGreater(a['coverage']['coverage_ratio'],.55)
        self.assertLess(a['coverage']['repeated_scan_fraction'],.30)
        owners=[]
        for name,building in a['buildings'].items():
            uid=str(building['uav_id']);owners.append(uid);point=Point(building['point_m'])
            task=p['planned_uavs'][uid]['task'];region=Polygon(task['polygon_m'])
            self.assertTrue(region.contains(point))
            self.assertGreater(point.distance(region.boundary),10.)
            self.assertLess(point.distance(Point(task['waypoints_m'][0])),task['reconnaissance_radius_m'])
            self.assertEqual(building['latitude'],BUILDINGS[name]['latitude'])
            self.assertEqual(building['longitude'],BUILDINGS[name]['longitude'])
        self.assertEqual(len(set(owners)),3)
        validate_entries(a['points_m'],{uid:a['departure_point_m'] for uid in entries},entries,a['departure_point_m'])

    def test_rebalance_skips_an_unreachable_optional_point(self):
        from competition_backend.subject2_rebalance import rebalance
        from competition_backend.subject1_actual_scene import RADII_BY_UAV, REFERENCE_HEIGHTS_BY_UAV
        a=self.plan['search_area'];info=a['western_rebalance']
        routes=[copy.deepcopy(self.plan['planned_uavs'][str(uid)]['task']) for uid in range(1,7)]
        transferred=info['transferred_waypoints_m']
        routes[2]['waypoints_m']=[q for q in routes[2]['waypoints_m'] if q not in transferred and q!=info['optional_point_m']]
        routes[4]['waypoints_m'].extend(transferred)
        regions=[Polygon(t['polygon_m']) for t in routes]
        regions[2]=Polygon(info['original_priority_polygon_m'])
        shared=Polygon(a['cooperative_region']['polygon_m'])
        regions[4]=shared.difference(regions[2])
        boundary=Polygon(a['points_m'])
        result=rebalance(routes,regions,a['points_m'],Polygon(a['cooperative_region']['polygon_m']),
            shape(a['terrain_layers_m']['required']),boundary.buffer(-5.03,join_style=2),
            shape(a['terrain_layers_m']['scan_forbidden']),a['departure_point_m'],[10000.,10000.],
            RADII_BY_UAV,REFERENCE_HEIGHTS_BY_UAV,update_timing)
        planned,partitions,_,_,meta=result
        self.assertFalse(meta['optional_included'])
        self.assertEqual([len(planned[i]['waypoints_m']) for i in (2,4)],[5,6])
        self.assertEqual(planned[2]['waypoints_m'][0][0],min(q[0] for q in planned[2]['waypoints_m']))
        self.assertTrue(all(t['mission_time_s']<=840. for t in planned))
        self.assertTrue(meta['subregions_disjoint'])
        self.assertTrue(all(a.intersection(b).area<1e-6 for a,b in itertools.combinations(partitions,2)))

    def test_api_defaults_fixed_geography_and_invalid_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':directory,'COMPETITION_DIAGNOSTICS_DIR':directory})
            try:
                client=TestClient(app)
                payload=dict(subject='subject2',planning_mode='competition',flight_profile=PROFILE,
                    coordinate_mode='gps',departure_point='stadium_center',controller_mode='external',
                    gps_origin=dict(latitude=39.,longitude=121.))
                with patch('competition_backend.subject2_actual_scene.prepare_plan',side_effect=AssertionError('must load saved plan')):
                    response=client.post('/api/v1/planning/competition-coverage',json=payload)
                    self.assertEqual(response.status_code,200,response.text)
                    result=response.json()
                    self.assertEqual(result['search_area']['points'],self.plan['search_area']['points'])
                    for uid,item in result['planned_uavs'].items():
                        expected=self.plan['planned_uavs'][uid]['task']
                        self.assertEqual(item['task']['waypoints_m'],expected['waypoints_m'])
                        self.assertEqual(item['task']['polygon_m'],expected['polygon_m'])
                        self.assertEqual(item['task']['scan_visit_count'],expected['scan_visit_count'])
                    dispatched=client.post('/api/v1/plan',json=payload)
                    self.assertEqual(dispatched.status_code,200,dispatched.text)
                commands=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                self.assertEqual(len(commands),6)
                for command in commands:
                    uid=command['uav_id'];wire=dict(command['payload'],type='assign_task',uav_id=uid)
                    checked=validate_assignment(wire,uid)
                    self.assertEqual(checked['task']['hover_scan_seconds'],30.)
                    self.assertEqual(checked['target_altitude_m'],[59,56,53,50,47,44][uid-1])
                    entry,returns=route_messages(checked)
                    self.assertEqual(len(returns['routes']),self.plan['search_area']['coverage']['total_scan_count'])
                    self.assertEqual(entry['path'][0],returns['landing_point'])
                for changes in (dict(coordinate_mode='xyz'),dict(departure_point='southeast'),
                                dict(hover_scan_seconds=40),dict(speed_mps=6),dict(reconnaissance_radius_m=45)):
                    response=client.post('/api/v1/planning/competition-coverage',json=dict(payload,**changes))
                    self.assertIn(response.status_code,(409,422),response.text)
            finally:
                app.state.audit.close()

    def test_actual_fleet_entries_and_time_are_checked_before_any_send(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':directory,'COMPETITION_DIAGNOSTICS_DIR':directory})
            try:
                p=copy.deepcopy(self.plan);a=p['search_area'];a['takeoff_gps_by_uav']=homes_for(p)
                o=app.state.orchestrator
                result=o.plan('subject2',flight_profile=PROFILE,prepared_plan=p,controller_mode='external')
                commands=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                self.assertEqual(len(commands),6)
                projection=a['coverage']['projection']
                def local(point):
                    return [(point[1]-projection['latitude'])*projection['north_m_per_degree'],
                            -(point[0]-projection['longitude'])*projection['west_m_per_degree']]
                entries={};homes={}
                for command in commands:
                    uid=command['uav_id'];payload=command['payload'];task=payload['task']
                    routes=task['transit_routes'];entries[uid]=[local(q) for q in routes['entry_path']]
                    homes[uid]=local(routes['departure'])
                    own_return=next(r['path'] for r in routes['return_paths'] if r['source_uav_id']==uid and r['waypoint_index']==1)
                    flight=[*entries[uid],*task['waypoints_m'][1:],*task['waypoints_m'][-2::-1],*[local(q) for q in own_return[1:]]]
                    expected=LineString(flight).length/5+60*len(task['waypoints_m'])+payload['target_altitude_m']/6
                    self.assertAlmostEqual(expected,task['mission_time_s'],places=5)
                    self.assertLessEqual(expected,840.)
                    self.assertEqual(task['waypoints_m'],self.plan['planned_uavs'][str(uid)]['task']['waypoints_m'])
                    for route in routes['return_paths']:
                        self.assertEqual(route['path'][-1],routes['departure'])
                validate_entries(a['points_m'],homes,entries,a['departure_point_m'])
                app.state.adapter.commands.clear()
                broken=copy.deepcopy(p)
                broken['search_area']['takeoff_gps_by_uav']['6']=dict(a['takeoff_gps_by_uav']['1'])
                with self.assertRaises(ValueError):
                    o.plan('subject2',flight_profile=PROFILE,prepared_plan=broken,controller_mode='external')
                self.assertFalse(app.state.adapter.commands)
                # A configured high climb must fail the full budget before the first assignment.
                old=copy.deepcopy(o.config.subjects['subject2']['takeoff_altitudes_m_by_profile']['around54m'])
                try:
                    o.config.subjects['subject2']['takeoff_altitudes_m_by_profile']['around54m']={str(uid):6000.+uid for uid in range(1,7)}
                    with self.assertRaises(ValueError):
                        o.plan('subject2',flight_profile=PROFILE,prepared_plan=p,controller_mode='external')
                    self.assertFalse(app.state.adapter.commands)
                finally:
                    o.config.subjects['subject2']['takeoff_altitudes_m_by_profile']['around54m']=old
            finally:
                app.state.audit.close()


if __name__=='__main__':unittest.main()
