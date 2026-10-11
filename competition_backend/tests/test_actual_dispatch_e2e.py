"""Offline six-ground dispatch -> onboard acceptance -> actual ROS publication methods.

No sockets, SSH, aircraft, ROS master or motion commands are used by this test.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
for relative in ('src/su17_competition_executor/test', 'src/su17_competition_executor/src'):
    sys.path.insert(0, str(ROOT / relative))
import test_executor_autonomy as autonomy
from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.models import Telemetry
from competition_backend.subject1_actual_scene import load_plan, PROFILE
from competition_shared.fleet import default_fleet, apply_fixed_binding
from su17_competition_executor.task_protocol import validate_assignment, load_assignment


class ActualDispatchE2ETest(unittest.TestCase):
    def test_web_selection_six_ground_nodes_onboard_cache_and_all_program_b_topics(self):
        fixed = load_plan()
        expected_counts = [8, 11, 8, 12, 16, 12]
        heights = [59., 56., 53., 50., 47., 44.]
        radii = [61., 58., 55., 52., 50., 45.]
        area = fixed['search_area']; projection = area['coverage']['projection']; departure = area['departure_point_m']
        homes = {uid: dict(latitude=projection['latitude'] + (departure[0]+(uid-3.5)*10)/projection['north_m_per_degree'],
                           longitude=projection['longitude']-departure[1]/projection['west_m_per_degree'],
                           boot_id='offline-boot-'+str(uid), source='first_valid_unarmed_gps_per_boot') for uid in range(1,7)}
        artifacts, timings, nodes = [], [], {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); clients, apps = {}, {}
            config = default_fleet()
            for uid in range(1,7): config = apply_fixed_binding(config,uid,'p600')
            def network(url, method='GET', payload=None, timeout=2.0):
                for uid, client in clients.items():
                    prefix = 'http://192.168.2.%d:8000' % (197+5*uid)
                    if url.startswith(prefix+'/'):
                        response = client.request(method,url[len(prefix):],json=payload,headers={'X-Competition-Peer-Token':'offline-peer'})
                        if response.status_code>=400: raise RuntimeError(response.text)
                        return response.json()
                raise AssertionError('Unexpected remote request: '+url)
            try:
                for uid in range(1,7):
                    folder=root/str(uid);folder.mkdir()
                    fleet=folder/'fleet.json';fleet.write_text(json.dumps(config),encoding='utf-8')
                    app=create_app(dict(COMPETITION_ADAPTER='distributed',COMPETITION_FLEET_CONFIG=str(fleet),
                        COMPETITION_DATA_DIR=str(folder),COMPETITION_DIAGNOSTICS_DIR=str(folder/'logs'),
                        COMPETITION_IMAGE_ROOT=str(folder/'images'),COMPETITION_GROUND_TERMINAL_ID=str(uid),
                        COMPETITION_PEER_TOKEN='offline-peer',COMPETITION_ASYNC_OPERATOR_SELECTION='0',
                        COMPETITION_CONFIRM_LIVE_CONFIG='1'))
                    app.state.program_manager=Mock()
                    app.state.program_manager.snapshot.return_value={'programs':{'flight':{'state':'running','flight_mode':'outdoor'}}}
                    app.state.adapter._request_json=network
                    app.state.adapter.local_adapter.forward_command=Mock()
                    apps[uid]=app;clients[uid]=TestClient(app) # No lifespan/network workers.
                for uid in [2,3,4,5,6,1]:
                    response=clients[uid].post('/api/v1/operator',json=dict(ground_terminal_id=uid,model='p600',task_publisher=uid==1))
                    self.assertEqual(response.status_code,200,response.text)
                publisher=apps[1]
                publisher.state.adapter.peer_program_b_status=Mock(side_effect=lambda:{str(uid):{
                    'state':'running','received_at':time.time(),'flight_mode':'outdoor'} for uid in range(2,7)})
                publisher.state.adapter.connected_uav_ids_snapshot=Mock(return_value=list(range(1,7)))
                for uid, home in homes.items():
                    publisher.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,received_at=publisher.state.orchestrator.clock(),
                        connected=True,latitude=home['latitude']+.0001,longitude=home['longitude'],altitude=103.7,
                        gps_status=3,location_source=4,gps_telemetry_age_seconds=60.,
                        capabilities=dict(boot_id=home['boot_id'],boot_gps_home=home)))
                selection=dict(subject='subject1',planning_mode='competition',flight_profile=PROFILE,
                    coordinate_mode='gps',departure_point='stadium_center',controller_mode='external',
                    flight_altitude_plan='around54m',reconnaissance_radius_m=61.,speed_mps=5.,hover_scan_seconds=40.,
                    recognition_selection=dict(category_ids=[],category_count=0))
                with patch('competition_backend.subject1_actual_scene.prepare_plan',side_effect=AssertionError('must use fixed plan')):
                    preview=clients[1].post('/api/v1/planning/competition-coverage',json=selection)
                    self.assertEqual(preview.status_code,200,preview.text)
                    # Same asynchronous endpoint as the page, under the unchanged 45-second deadline.
                    for attempt in (1,2):
                        started=time.monotonic()
                        response=clients[1].post('/api/v1/plan/jobs',json=dict(selection,request_id='offline-'+str(attempt)))
                        self.assertEqual(response.status_code,200,response.text)
                        job=response.json()
                        while job['state'] in ('queued','running') and time.monotonic()-started<45:
                            time.sleep(.02)
                            job=clients[1].get('/api/v1/plan/jobs/'+job['job_id']).json()
                        self.assertEqual(job['state'],'succeeded',job.get('error'))
                        elapsed=time.monotonic()-started
                        timings.append(elapsed)
                        self.assertLess(elapsed,15.,'Six-aircraft planning must leave ample time for transport')
                        self.assertEqual(job['result']['dispatch_status']['assigned_uav_ids'],list(range(1,7)))
                        if attempt==1:first_mission_id=job['result']['mission']['mission_id']
                self.assertNotEqual(first_mission_id,job['result']['mission']['mission_id'])
                premature=clients[1].post('/api/v1/takeoff/prepare')
                self.assertEqual(premature.status_code,200,premature.text)
                self.assertFalse(premature.json()['ready'])
                for uid,app in apps.items():
                    forward=app.state.adapter.local_adapter.forward_command
                    self.assertEqual(forward.call_count,2)
                    actual_uid,kind,payload=forward.call_args.args
                    self.assertEqual((actual_uid,kind),(uid,'assign_task'))
                    # Exactly the JSON bytes that can cross the wire, with a verified complete checksum.
                    wire=json.loads(json.dumps(dict(payload,type=kind,uav_id=uid),allow_nan=False))
                    checked=validate_assignment(wire,uid)
                    expected=fixed['planned_uavs'][str(uid)]['task']
                    self.assertEqual(checked['task']['waypoints_wgs84'],expected['waypoints_wgs84'])
                    self.assertEqual(checked['task']['waypoints_m'],expected['waypoints_m'])
                    self.assertEqual(checked['task']['polygon_m'],expected['polygon_m'])
                    self.assertEqual(preview.json()['planned_uavs'][str(uid)]['task']['waypoints_wgs84'],expected['waypoints_wgs84'])
                    self.assertEqual(len(expected['waypoints_m']),expected_counts[uid-1])
                    self.assertEqual(checked['target_altitude_m'],heights[uid-1])
                    self.assertEqual(checked['task']['reconnaissance_radius_m'],radii[uid-1])
                    self.assertNotIn('speed_mps',checked['task'])
                    node=autonomy.AutonomyTest().executor(root/str(uid))
                    node.uav_id=uid;node._assignment=None;node._assignment_acked=False
                    node.max_distance_from_home=1500.;node.max_speed=2.;node.log_full_task=False
                    node.recognition_categories_pub=Mock();node.recognition_categories_topic='/uav%d/competition/recognition_categories'%uid
                    node.task_region_pub=Mock();node.task_region_topic='/uav%d/competition/task_region'%uid
                    node.external_entry_path_pub=Mock();node.external_return_paths_pub=Mock()
                    node._publish_successful_return=Mock()
                    state=SimpleNamespace(armed=False,odom_valid=True,gps_status=3,location_source=4,
                        latitude=homes[uid]['latitude'],longitude=homes[uid]['longitude'],altitude=103.7,
                        position=[0.,0.,0.],velocity=[0.,0.,0.],attitude=[0.,0.,0.])
                    node._state=state;node._snapshot=lambda state=state:(state,None)
                    node._accept_assignment(wire)
                    self.assertTrue(node._assignment_acked,str(node._publish_status.call_args_list))
                    self.assertEqual(load_assignment(node.cache_path),checked)
                    node.external_path_pub.publish.assert_not_called() # Assignment must not start B.
                    node.external_entry_path_pub.publish.assert_not_called()
                    self.assertEqual(node.recognition_categories_pub.publish.call_args.args[0].data,[])
                    nodes[uid]=(node,checked,state,expected)
                    # Accepted onboard assignment produces the current mission/checksum receipt.
                    publisher.state.orchestrator.update_telemetry(Telemetry(uav_id=uid,
                        received_at=time.time(),connected=True,armed=False,odom_valid=True,failsafe=False,
                        control_state=0,battery_percentage=.95,latitude=state.latitude,longitude=state.longitude,
                        gps_status=3,location_source=4,gps_telemetry_age_seconds=60.,
                        identity={'identity_verified':True},capabilities={'motion_enabled':True,
                        'auto_takeoff_supported':True,'auto_takeoff_ready':True},
                        task_assignment_acked=node._assignment_acked,task_assignment_mission_id=checked['mission_id'],
                        task_assignment_checksum=checked['assignment_checksum']))
                prepared=clients[1].post('/api/v1/takeoff/prepare')
                self.assertEqual(prepared.status_code,200,prepared.text)
                self.assertTrue(prepared.json()['ready'],prepared.text)
                confirmed=clients[1].post('/api/v1/takeoff/confirm',json={'token':prepared.json()['confirmation_token']})
                self.assertEqual(confirmed.status_code,200,confirmed.text)
                self.assertEqual(confirmed.json()['mission']['phase'],'taking_off')
                for uid,(node,checked,state,expected) in nodes.items():
                    calls=apps[uid].state.adapter.local_adapter.forward_command.call_args_list
                    self.assertEqual([call.args[1] for call in calls],['assign_task','assign_task','takeoff'])
                    takeoff=calls[-1].args[2]
                    self.assertEqual(takeoff['mission_id'],checked['mission_id'])
                    self.assertEqual(takeoff['target_altitude_m'],heights[uid-1])
                    # Exercise the actual post-takeoff handoff, while stubbing only flight itself.
                    node._motion_entry('takeoff',lambda _:False,takeoff)
                    node.external_path_pub.publish.assert_not_called()
                    node._gps_home=(state.latitude,state.longitude,state.altitude)
                    node._motion_entry('takeoff',lambda _:True,takeoff)
                    node.external_path_pub.publish.assert_called_once()
                    path=node.external_path_pub.publish.call_args.args[0].data
                    expected_path=[v for lat,lon in expected['waypoints_wgs84'] for v in (lon,lat,heights[uid-1])]
                    self.assertEqual(path,expected_path)
                    landing=node.external_landing_pub.publish.call_args.args[0].data
                    self.assertEqual(landing,[homes[uid]['longitude'],homes[uid]['latitude'],heights[uid-1]])
                    entry=json.loads(node.external_entry_path_pub.publish.call_args.args[0].data)
                    returns=json.loads(node.external_return_paths_pub.publish.call_args.args[0].data)
                    self.assertEqual(entry['path'][0],landing);self.assertEqual(entry['path'][-1],path[:3])
                    self.assertEqual(len(returns['routes']),67)
                    for route in returns['routes']:
                        source=fixed['planned_uavs'][str(route['source_uav_id'])]['task']['waypoints_wgs84'][route['waypoint_index']-1]
                        self.assertEqual(route['path'][0],[source[1],source[0],heights[uid-1]])
                        self.assertEqual(route['path'][-1],landing)
                        self.assertTrue(all(p[2]==heights[uid-1] for p in route['path']))
                    node._fly_to.assert_not_called();node._land.assert_not_called()
                    self.assertEqual(node.recon_start_mode_pub.publish.call_args.args[0].data,0)
                    artifacts.append(dict(uav_id=uid,height_m=heights[uid-1],radius_m=radii[uid-1],waypoint_count=len(path)//3,
                        assignment=checked,path_stage_1=path,jiangluodian=landing,entry_path=entry,return_paths=returns))
            finally:
                for app in apps.values():app.state.audit.close()
        output=os.environ.get('COMPETITION_OFFLINE_EVIDENCE')
        if output:
            Path(output).write_text(json.dumps(dict(offline_only=True,simulated_homes=True,profile=PROFILE,
                altitude_plan='around54m',dispatch_seconds=timings,takeoff_api_confirmed=True,aircraft=artifacts),ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':unittest.main()
