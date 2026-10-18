"""三场景的真实 API -> 六地面转发 -> 机载缓存 -> 起飞后 ROS 发布；全部离线。"""
import copy
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
for part in ('src/su17_competition_executor/test', 'src/su17_competition_executor/src'):
    sys.path.insert(0, str(ROOT / part))
import test_executor_autonomy as autonomy
from fastapi.testclient import TestClient
from shapely.geometry import Polygon, LineString, Point
from shapely.ops import unary_union
from competition_backend.api import create_app
from competition_backend.final_scenes import PROFILES, load_plan, departure_for, altitude_profile
from competition_backend.models import Telemetry
from competition_backend.transit_preferences import routes_from_home, prefer_entries, update_subject2_timing
from competition_backend.transit_routes import rebase_gps_routes_for_takeoff
from competition_shared.fleet import default_fleet, apply_fixed_binding
from su17_competition_executor.task_protocol import validate_assignment, load_assignment


class FinalScenesTest(unittest.TestCase):
    def test_scene_parameters_default_dispatch_and_stale_values(self):
        for profile in PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                app = create_app(dict(COMPETITION_ADAPTER='sim',COMPETITION_DATA_DIR=directory,
                    COMPETITION_DIAGNOSTICS_DIR=directory,COMPETITION_IMAGE_ROOT=directory))
                try:
                    client = TestClient(app)
                    seconds = 50. if profile == 'final_scene_1' else 30.
                    selection = dict(subject='subject2',planning_mode='competition',flight_profile=profile,
                        coordinate_mode='gps',departure_point=departure_for(profile),controller_mode='external',
                        flight_altitude_plan=altitude_profile(profile))
                    for stale in (dict(hover_scan_seconds=30. if seconds==50. else 50.),
                                  dict(speed_mps=4.),dict(reconnaissance_radius_m=30.)):
                        for endpoint in ('/api/v1/planning/competition-coverage','/api/v1/plan'):
                            rejected=client.post(endpoint,json=dict(selection,**stale))
                            self.assertEqual(rejected.status_code,422,rejected.text)
                            self.assertIn('{:g}秒/点'.format(seconds),rejected.json()['detail'])
                    self.assertFalse(any(c['type']=='assign_task' for c in app.state.adapter.snapshot()))
                    preview=client.post('/api/v1/planning/competition-coverage',json=selection)
                    self.assertEqual(preview.status_code,200,preview.text)
                    self.assertEqual(preview.json()['search_area']['coverage']['hover_scan_seconds'],seconds)
                    result=client.post('/api/v1/plan',json=selection)
                    self.assertEqual(result.status_code,200,result.text)
                    sent=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                    self.assertEqual(len(sent),6)
                    for command in sent:
                        task=command['payload']['task']
                        self.assertEqual(task['hover_scan_seconds'],seconds)
                        for layer in task.get('layers',[]):
                            self.assertEqual(layer['hover_scan_seconds'],seconds)
                finally:
                    app.state.audit.close()

    def test_three_scenes_six_ground_assign_takeoff_ros(self):
        evidence = []
        for profile in PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                fixed = load_plan(profile); area = fixed['search_area']; projection = area['coverage']['projection']
                departure = area['departure_point_m']
                homes = {uid: dict(latitude=projection['latitude']+(departure[0]+(uid-3.5)*2.)/projection['north_m_per_degree'],
                    longitude=projection['longitude']-departure[1]/projection['west_m_per_degree'],
                    boot_id='offline-'+str(uid), source='first_valid_unarmed_gps_per_boot') for uid in range(1,7)}
                clients, apps, nodes, aircraft = {}, {}, {}, []
                fleet = default_fleet()
                for uid in range(1,7):
                    fleet = apply_fixed_binding(fleet, uid, 'p600')
                def network(url, method='GET', payload=None, timeout=2.):
                    for uid,client in clients.items():
                        prefix = 'http://192.168.2.%d:8000' % (197+5*uid)
                        if url.startswith(prefix+'/'):
                            response = client.request(method,url[len(prefix):],json=payload,headers={'X-Competition-Peer-Token':'offline'})
                            if response.status_code >= 400:
                                raise RuntimeError(response.text)
                            return response.json()
                    raise AssertionError('No real network allowed: '+url)
                try:
                    for uid in range(1,7):
                        folder = Path(directory)/str(uid); folder.mkdir()
                        path = folder/'fleet.json'; path.write_text(json.dumps(fleet),encoding='utf8')
                        app = create_app(dict(COMPETITION_ADAPTER='distributed',COMPETITION_FLEET_CONFIG=str(path),
                            COMPETITION_DATA_DIR=str(folder),COMPETITION_DIAGNOSTICS_DIR=str(folder/'logs'),
                            COMPETITION_IMAGE_ROOT=str(folder/'images'),COMPETITION_GROUND_TERMINAL_ID=str(uid),
                            COMPETITION_PEER_TOKEN='offline',COMPETITION_ASYNC_OPERATOR_SELECTION='0',COMPETITION_CONFIRM_LIVE_CONFIG='1'))
                        app.state.program_manager = Mock()
                        app.state.program_manager.snapshot.return_value = {'programs':{'flight':{'state':'running','flight_mode':'outdoor'}}}
                        app.state.adapter._request_json = network
                        app.state.adapter.local_adapter.forward_command = Mock()
                        apps[uid] = app; clients[uid] = TestClient(app)
                    for uid in [2,3,4,5,6,1]:
                        r = clients[uid].post('/api/v1/operator',json=dict(ground_terminal_id=uid,model='p600',task_publisher=uid==1))
                        self.assertEqual(r.status_code,200,r.text)
                    publisher = apps[1]
                    publisher.state.adapter.peer_program_b_status = Mock(return_value={str(uid):{'state':'running','received_at':time.time(),'flight_mode':'outdoor'} for uid in range(2,7)})
                    publisher.state.adapter.connected_uav_ids_snapshot = Mock(return_value=list(range(1,7)))
                    for uid,home in homes.items():
                        publisher.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,received_at=time.time(),connected=True,odom_valid=True,
                            latitude=home['latitude'],longitude=home['longitude'],gps_status=3,location_source=4,
                            capabilities=dict(boot_id=home['boot_id'],boot_gps_home=home)))
                    selection = dict(subject='subject2',planning_mode='competition',flight_profile=profile,coordinate_mode='gps',
                        departure_point=departure_for(profile),controller_mode='external',flight_altitude_plan=altitude_profile(profile),
                        reconnaissance_radius_m=50.,speed_mps=5.,hover_scan_seconds=50. if profile=='final_scene_1' else 30.,duration_seconds=1.,
                        recognition_selection={'category_ids':[],'category_count':0})
                    preview = clients[1].post('/api/v1/planning/competition-coverage',json=selection)
                    self.assertEqual(preview.status_code,200,preview.text)
                    self.assertEqual(preview.json()['search_area']['coverage']['hover_scan_seconds'],selection['hover_scan_seconds'])
                    started = time.monotonic()
                    r = clients[1].post('/api/v1/plan/jobs',json=dict(selection,request_id='final-'+profile))
                    self.assertEqual(r.status_code,200,r.text); job = r.json()
                    while job['state'] in ('queued','running') and time.monotonic()-started < 30:
                        time.sleep(.02); job = clients[1].get('/api/v1/plan/jobs/'+job['job_id']).json()
                    self.assertEqual(job['state'],'succeeded',job.get('error'))
                    elapsed = time.monotonic()-started
                    self.assertLess(elapsed,15.)
                    self.assertEqual(job['result']['dispatch_status']['assigned_uav_ids'],list(range(1,7)))
                    self.assertEqual(job['result']['mission']['duration_seconds'],0.)
                    for uid,app in apps.items():
                        actual_uid,kind,payload = app.state.adapter.local_adapter.forward_command.call_args.args
                        self.assertEqual((actual_uid,kind),(uid,'assign_task'))
                        wire = json.loads(json.dumps(dict(payload,type=kind,uav_id=uid),allow_nan=False))
                        checked = validate_assignment(wire,uid)
                        expected = fixed['planned_uavs'][str(uid)]['task']
                        for name in ('waypoints_m','waypoints_wgs84','polygon_m'):
                            self.assertEqual(checked['task'][name],expected[name])
                        expected_heights = {'final_scene_1': (47,50,53,34,37,40),
                                            'final_scene_2': (47,49,51,53,55,57),
                                            'xuchang_small_2': (47,50,53,47,50,53)}
                        height = expected_heights[profile][uid-1]
                        self.assertEqual(checked['target_altitude_m'],height)
                        if profile != 'final_scene_1':
                            self.assertEqual(checked['task']['layers'],expected['layers'])
                            self.assertEqual(checked['task']['layers'][0]['altitude_m'],height)
                            self.assertEqual(checked['task']['layers'][1]['altitude_m'],(34,37,40,34,37,40)[uid-1])
                        node = autonomy.AutonomyTest().executor(Path(directory)/str(uid))
                        node.uav_id=uid; node._assignment=None; node._assignment_acked=False
                        node.max_distance_from_home=1500.; node.max_speed=2.; node.log_full_task=False
                        for name in ('recognition_categories','task_region','fixed_targets','external_entry_path','external_return_paths'):
                            setattr(node,name+'_pub',Mock())
                            setattr(node,name+'_topic','/uav%d/competition/%s' % (uid,name))
                        node._publish_successful_return=Mock()
                        node.external_path_topic='/ground_mission_planner/vehicle_%d/path_stage_1'%uid
                        node.external_lower_path_topic='/ground_mission_planner/vehicle_%d/path_stage_2'%uid
                        state=SimpleNamespace(armed=False,odom_valid=True,gps_status=3,location_source=4,
                            latitude=homes[uid]['latitude'],longitude=homes[uid]['longitude'],altitude=103.7,
                            position=[0.,0.,0.],velocity=[0.,0.,0.],attitude=[0.,0.,0.])
                        node._state=state;node._snapshot=lambda state=state:(state,None)
                        node._accept_assignment(wire)
                        self.assertTrue(node._assignment_acked,str(node._publish_status.call_args_list))
                        self.assertEqual(load_assignment(node.cache_path),checked)
                        node.external_path_pub.publish.assert_not_called()
                        node.external_lower_path_pub.publish.assert_not_called()
                        region=json.loads(node.task_region_pub.publish.call_args.args[0].data)
                        self.assertEqual(region['assignment_checksum'],checked['assignment_checksum'])
                        nodes[uid]=(node,checked,state,expected)
                        publisher.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,received_at=time.time(),connected=True,
                            armed=False,odom_valid=True,failsafe=False,control_state=0,battery_percentage=.95,
                            latitude=state.latitude,longitude=state.longitude,gps_status=3,location_source=4,
                            identity={'identity_verified':True},capabilities={'motion_enabled':True,'auto_takeoff_supported':True,'auto_takeoff_ready':True},
                            task_assignment_acked=True,task_assignment_mission_id=checked['mission_id'],task_assignment_checksum=checked['assignment_checksum']))
                    r=clients[1].post('/api/v1/takeoff/prepare'); self.assertEqual(r.status_code,200,r.text)
                    self.assertTrue(r.json()['ready'],r.text)
                    r=clients[1].post('/api/v1/takeoff/confirm',json={'token':r.json()['confirmation_token']})
                    self.assertEqual(r.status_code,200,r.text)
                    for uid,(node,checked,state,expected) in nodes.items():
                        takeoff=apps[uid].state.adapter.local_adapter.forward_command.call_args.args[2]
                        self.assertIsNone(takeoff['deadline_at'])
                        self.assertEqual(takeoff['target_altitude_m'],checked['target_altitude_m'])
                        node._motion_entry('takeoff',lambda _:False,takeoff)
                        node.external_path_pub.publish.assert_not_called()
                        node._gps_home=(state.latitude,state.longitude,state.altitude)
                        node._motion_entry('takeoff',lambda _:True,takeoff)
                        stage1=node.external_path_pub.publish.call_args.args[0].data
                        stage2=node.external_lower_path_pub.publish.call_args.args[0].data
                        height=checked['target_altitude_m']
                        self.assertEqual(stage1,[v for lat,lon in expected['waypoints_wgs84'] for v in (lon,lat,height)])
                        lower=expected.get('layers',[None,None])[1]
                        self.assertEqual(stage2,[] if not lower else [v for lat,lon in lower['waypoints_wgs84'] for v in (lon,lat,lower['altitude_m'])])
                        returned=json.loads(node.external_return_paths_pub.publish.call_args.args[0].data)
                        landing=node.external_landing_pub.publish.call_args.args[0].data
                        self.assertEqual(landing,[homes[uid]['longitude'],homes[uid]['latitude'],height])
                        self.assertEqual(len(returned['routes']),sum(len(v['task']['waypoints_m']) for v in fixed['planned_uavs'].values()))
                        for route in returned['routes']:
                            source=fixed['planned_uavs'][str(route['source_uav_id'])]['task']['waypoints_wgs84'][route['waypoint_index']-1]
                            self.assertEqual(route['path'][0],[source[1],source[0],height])
                            self.assertEqual(route['path'][-1],landing)
                            self.assertTrue(all(p[2]==height for p in route['path']))
                        manifest=json.loads(node.external_mission_pub.publish.call_args.args[0].data)
                        self.assertEqual(manifest['speed_mps'],5.)
                        self.assertEqual(manifest['hover_scan_seconds'],50. if not lower else 30.)
                        self.assertIsNone(manifest['deadline_at'])
                        self.assertEqual(manifest['layers'][0]['scan_passes'],2)
                        if lower:
                            self.assertEqual(manifest['execution_mode'],'upper_roundtrip_then_lower_roundtrip')
                            self.assertEqual(manifest['layers'][1]['scan_passes'],2)
                            self.assertEqual(manifest['mission_budget_seconds'],960.)
                            for layer in manifest['layers']:
                                n=layer['waypoint_count']
                                self.assertEqual(layer['scan_execution_order'],list(range(n))+list(reversed(range(n))))
                        self.assertEqual(manifest['return_policy'],'climb_to_upper_then_return' if lower else 'at_task_altitude')
                        self.assertEqual(node.recon_start_mode_pub.publish.call_args.args[0].data,0)
                        node._fly_to.assert_not_called();node._land.assert_not_called()
                        aircraft.append(dict(uav_id=uid,assignment=checked,takeoff=takeoff,
                            task_region=json.loads(node.task_region_pub.publish.call_args.args[0].data),
                            fixed_targets=json.loads(node.fixed_targets_pub.publish.call_args.args[0].data),
                            competition_time=json.loads(node.competition_time_pub.publish.call_args.args[0].data),
                            recognition_categories=node.recognition_categories_pub.publish.call_args.args[0].data,
                            path_stage_1=stage1,path_stage_2=stage2,jiangluodian=landing,return_paths=returned,
                            entry_path=json.loads(node.external_entry_path_pub.publish.call_args.args[0].data),
                            external_mission=manifest,recon_start_mode=0))
                    evidence.append(dict(scene_id=profile,dispatch_seconds=elapsed,aircraft=aircraft,
                                         plan=job['result']['mission']))
                finally:
                    for app in apps.values():
                        app.state.audit.close()
        if os.environ.get('COMPETITION_FINAL_EVIDENCE'):
            Path(os.environ['COMPETITION_FINAL_EVIDENCE']).write_text(json.dumps(dict(offline_only=True,simulated_homes=True,scenes=evidence),ensure_ascii=False,indent=2),encoding='utf8')

    def test_fixed_geometry_and_actual_scan_connections(self):
        for profile in PROFILES:
            plan=load_plan(profile); area=plan['search_area']
            boundary=Polygon(area['points_m']); holes=unary_union([Polygon(r) for r in area['excluded_polygons_m']])
            tasks=[u['task'] for u in plan['planned_uavs'].values()]
            groups=('upper','lower') if profile=='final_scene_1' else ('all',)
            for group in groups:
                regions=[Polygon(t['polygon_m'],holes=t['polygon_holes_m']) for t in tasks if t['partition_group']==group]
                self.assertEqual(len(regions),3 if profile=='final_scene_1' else 6)
                for a,b in itertools.combinations(regions,2):
                    self.assertLess(a.intersection(b).area,1e-5)
            for task in tasks:
                region = Polygon(task['polygon_m'], holes=task['polygon_holes_m'])
                layers=task.get('layers') or [dict(layer_id=task['physical_layer'],waypoints_m=task['waypoints_m'],reconnaissance_radius_m=task['reconnaissance_radius_m'])]
                for layer in layers:
                    self.assertEqual(layer['reconnaissance_radius_m'],50 if layer['layer_id']=='upper' else 30)
                    for point in layer['waypoints_m']:
                        self.assertTrue(region.buffer(1e-5).covers(Point(point)))
                        for ring in area['scan_exclusion_polygons_m']:
                            self.assertGreaterEqual(Point(point).distance(Polygon(ring))+1e-6,layer['reconnaissance_radius_m'])
                    for a,b in zip(layer['waypoints_m'],layer['waypoints_m'][1:]):
                        line=LineString([a,b]); self.assertTrue(boundary.covers(line))
                        self.assertGreaterEqual(line.distance(boundary.boundary)+1e-6,5.)
                        if not holes.is_empty:self.assertGreaterEqual(line.distance(holes)+1e-6,5.)
                if len(layers) == 2:
                    self.assertEqual(layers[0]['waypoints_m'][0],layers[1]['waypoints_m'][0])
                    self.assertEqual(task['mission_budget_seconds'],960.)
                    self.assertIsNone(task['mission_time_limit_s'])
                    if profile == 'xuchang_small_2':
                        self.assertLessEqual(task['mission_time_s'],960.)
                    self.assertAlmostEqual(sum(task['timing_components'].values()),task['mission_time_s'])
                    self.assertEqual(task['scan_visit_count'],2*sum(len(l['waypoints_m']) for l in layers))
                    self.assertEqual(task['hover_scan_time_s'],30*task['scan_visit_count'])
                    self.assertEqual(task['layer_transition']['from_waypoint_id'],layers[0]['waypoint_ids'][0])
                    self.assertEqual(task['normal_return_source'],dict(source_uav_id=task['sector'],waypoint_index=1))
                    for layer in layers:
                        self.assertGreaterEqual(len(layer['waypoints_m']),2)
                    connection=Point(layers[0]['waypoints_m'][0])
                    self.assertTrue(boundary.covers(connection))
                    self.assertGreaterEqual(connection.distance(boundary.boundary)+1e-6,5.)
                    if not holes.is_empty:self.assertGreaterEqual(connection.distance(holes)+1e-6,5.)

    def test_impossible_soft_constraints_and_long_duration_do_not_block(self):
        boundary=[[0,0],[100,0],[100,100],[0,100]]
        paths,metadata=routes_from_home(boundary,[20,20],[[80,80]],[[20,20]])
        self.assertTrue(metadata['home_avoidance_relaxed'])
        entries,info=prefer_entries(boundary,{1:paths[0],2:paths[0]},{1:[20,20],2:[20,20]})
        self.assertEqual(set(entries),{1,2});self.assertIsNone(info['required_inter_route_distance_m'])
        task={'waypoints_m':[[10,10],[15,15]]}
        update_subject2_timing(task,[[0,0],[10000,10000],[10,10]],[[10,10],[0,0]],59.)
        self.assertGreater(task['mission_time_s'],840.);self.assertIsNone(task['mission_time_limit_s'])
        plan=load_plan('final_scene_2');area=plan['search_area'];home=area['departure_point_wgs84']
        area['takeoff_gps_by_uav']={str(i):dict(home) for i in range(1,7)}
        before=copy.deepcopy(plan['planned_uavs']['1']['task']['waypoints_wgs84'])
        route=rebase_gps_routes_for_takeoff(area,plan['planned_uavs']['1']['task'],home)
        self.assertTrue(route['home_avoidance_relaxed'])
        self.assertEqual(plan['planned_uavs']['1']['task']['waypoints_wgs84'],before)


if __name__=='__main__': unittest.main()
