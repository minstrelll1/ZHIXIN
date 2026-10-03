import copy
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.tcp_adapter import TcpFleetAdapter
from competition_shared.fleet import FleetStore, apply_fixed_binding, default_fleet, validate_fleet, resolved_vehicle, validate_identity, revision
from competition_shared.runtime import ground_environment
from competition_shared.navigation import resolve_waypoints, wgs84_to_enu_xy
from competition_shared.scan import ScanSession
from test_tcp_adapter import send_message, receive_message, wait_until


class FleetConfigurationTest(unittest.TestCase):
    def test_p600_defaults_keep_ground_link_and_peer_networks_separate(self):
        config = default_fleet()
        for terminal_id in range(1, 7):
            config = apply_fixed_binding(config, terminal_id, 'p600')
        self.assertEqual(
            [v['ground_host'] for v in config['vehicles']],
            ['192.168.1.230'] * 6,
        )
        self.assertEqual(
            [v['peer_host'] for v in config['vehicles']],
            ['192.168.2.202', '192.168.2.207', '192.168.2.212',
             '192.168.2.217', '192.168.2.222', '192.168.2.227'],
        )

    def test_shared_radio_ip_keeps_routes_and_peer_addresses_distinct(self):
        config = default_fleet()
        for uid in range(1, 7):
            config = apply_fixed_binding(config, uid, 'p600')
        for uid in range(1, 7):
            local, env = ground_environment(config, uid)
            self.assertEqual(local['ground_host'], '192.168.1.230')
            self.assertEqual(env['COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP'], '192.168.1.230')
            self.assertEqual(env['COMPETITION_POINTCLOUD_CAPTURE_REMOTE_IP'], local['onboard_host'])
            self.assertEqual(env['COMPETITION_UAV_SSH_HOSTS'], f"{uid}={local['onboard_host']}")
            self.assertEqual(env['COMPETITION_GROUND_PEERS'], ';'.join(
                f"{v['uav_id']}=http://{v['peer_host']}:8000"
                for v in config['vehicles'] if v['uav_id'] != uid))
            self.assertNotIn('192.168.1.230', env['COMPETITION_GROUND_PEERS'])
        shipped = validate_fleet(json.loads((Path(__file__).resolve().parents[2] / 'config/fleet.json').read_text(encoding='utf-8-sig')))
        self.assertEqual([(v['onboard_host'],v['ground_host'],v['peer_host']) for v in shipped['vehicles']],
                         [(v['onboard_host'],v['ground_host'],v['peer_host']) for v in config['vehicles']])

    def test_su17_switch_uses_shared_radio_ip_and_keeps_peer(self):
        config = apply_fixed_binding(default_fleet(), 3, 'p600')
        config = apply_fixed_binding(config, 3, 'su17')
        v = config['vehicles'][2]
        self.assertEqual(v['onboard_host'], '192.168.1.88')
        self.assertEqual(v['ground_host'], '192.168.1.230')
        self.assertEqual(v['peer_host'], '192.168.2.212')
        v['ground_host'] = '192.168.1.123'
        with self.assertRaisesRegex(ValueError, '192.168.1.230'):
            validate_fleet(config)

    def test_standalone_launch_network_defaults_match_deployed_fleet(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ('src/su17_competition_executor/launch/onboard_competition_stack.launch',
                         'src/su17_competition_executor/launch/onboard_task_executor.launch',
                         'src/su17_image_transfer/launch/onboard_image_sender.launch'):
            with self.subTest(launch=relative):
                launch = ET.parse(root / relative).getroot()
                self.assertEqual(launch.find("arg[@name='ground_host']").get('default'), '192.168.1.230')

    def test_draft_does_not_guess_addresses_and_namespaces_follow_identity(self):
        config=validate_fleet(default_fleet())
        for uid in range(1, 7):
            v, env=ground_environment(config, uid)
            self.assertEqual(v['ros_namespace'], '/uav%d'%uid)
            self.assertEqual(env['COMPETITION_UAV_SSH_HOSTS'], '')
            self.assertEqual(env['COMPETITION_GROUND_PEERS'], '')
            self.assertEqual(env['COMPETITION_VIDEO_SOURCES'], '')
            self.assertEqual(env['COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP'], '')

    def test_port_and_device_collisions_rejected(self):
        for key,value in [('task_port',8000),('uav_id',2),('ground_terminal_id',2),('model','unknown'),('onboard_host','0.0.0.0'),('onboard_host',None),('max_speed_mps',float('nan'))]:
            config=default_fleet();config['vehicles'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):validate_fleet(config)
        config=default_fleet();config['vehicles'][0]['device_id']='one';config['vehicles'][1]['device_id']='one'
        with self.assertRaises(ValueError):validate_fleet(config)

    def test_persistence_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fleet.json';store=FleetStore(path);old=revision(store.read());config=store.read()
            config['vehicles'][2]['model']='su17';store.save(config,old)
            self.assertEqual(FleetStore(path).read()['vehicles'][2]['model'],'su17')
            with self.assertRaises(ValueError):store.save(default_fleet(),old)

    def test_independent_network_and_peer_addresses(self):
        config=default_fleet();one=config['vehicles'][0];one.update(onboard_host='192.168.1.88',ground_host='192.168.1.230',peer_host='10.20.0.1')
        config['vehicles'][1]['peer_host']='10.20.0.2'
        local,env=ground_environment(validate_fleet(config),1)
        self.assertEqual(env['COMPETITION_GROUND_PEERS'],'2=http://10.20.0.2:8000')
        self.assertEqual(env['COMPETITION_UAV_SSH_HOSTS'],'1=192.168.1.88')
        one['pointcloud_source']='onboard'
        self.assertEqual(ground_environment(config,1)[1]['COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP'],'')
        one['pointcloud_enabled']=False
        self.assertEqual(ground_environment(config,1)[1]['COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP'],'')

    def test_api_saves_candidate_with_auth_and_requires_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fleet.json';path.write_text(json.dumps(default_fleet()))
            env={'COMPETITION_FLEET_CONFIG':str(path),'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':tmp,'COMPETITION_PEER_TOKEN':'test-peer'}
            with patch.dict(os.environ,env),TestClient(create_app()) as client:
                data=client.get('/api/v1/fleet/config').json();data['config']['vehicles'][1]['model']='su17'
                payload={'config':data['config'],'revision':data['revision']}
                self.assertEqual(client.put('/api/v1/fleet/config',json=payload).status_code,403)
                result=client.put('/api/v1/fleet/config',json=payload,headers={'X-Competition-Peer-Token':'test-peer'})
                self.assertEqual(result.status_code,200,result.text)
                self.assertTrue(result.json()['restart_required'])
                self.assertEqual(client.post('/api/v1/plan',json={'subject':'subject1'}).status_code,409)
                self.assertEqual(client.get('/fleet').status_code,200)
                saved = FleetStore(path).read()['vehicles'][1]
                self.assertEqual(saved['model'],'su17')
                self.assertEqual(saved['onboard_host'],'192.168.1.88')
                self.assertEqual(saved['ground_host'],'192.168.1.230')


class MixedFleetTcpTest(unittest.TestCase):
    def setUp(self):
        self.config=default_fleet()
        for v in self.config['vehicles']:v['device_id']='device-%s'%v['uav_id']
        self.config['vehicles'][1]['model']='su17'
        self.adapter=TcpFleetAdapter([1,2],bind_host='127.0.0.1',port=0,auth_token='test',identity_validator=lambda h:validate_identity(h,self.config))
        self.adapter.start();self.clients=[]

    def tearDown(self):
        for client in self.clients:client.close()
        self.adapter.stop()

    def connect(self,uid,**overrides):
        client=socket.create_connection(('127.0.0.1',self.adapter.bound_port),timeout=2);client.settimeout(2)
        self.clients.append(client);buf=bytearray()
        hello=dict(type='hello',protocol_version=1,auth_token='test',uav_id=uid,flight_controller_id=uid,ros_namespace='/uav%d'%uid,model=self.config['vehicles'][uid-1]['model'],device_id='device-%d'%uid,identity_verified=True,fleet_revision=revision(self.config))
        hello.update(overrides);send_message(client,hello)
        return client,buf,receive_message(client,buf)

    def test_p600_and_su17_route_independently_and_preserve_metadata(self):
        one,b1,a1=self.connect(1);two,b2,a2=self.connect(2)
        self.assertEqual(a1['type'],'hello_ack');self.assertEqual(a2['type'],'hello_ack')
        self.adapter.command_assign_task(1,{'mission_id':'p600-task'})
        self.adapter.command_assign_task(2,{'mission_id':'su17-task'})
        self.assertEqual(receive_message(one,b1)['mission_id'],'p600-task')
        self.assertEqual(receive_message(two,b2)['mission_id'],'su17-task')
        self.assertEqual(self.adapter.telemetry_snapshot(1).identity['model'],'p600')
        self.assertEqual(self.adapter.telemetry_snapshot(2).identity['model'],'su17')

    def test_wrong_identity_does_not_displace_valid_drone(self):
        good,buf,_=self.connect(1)
        for bad in ({'model':'su17'},{'flight_controller_id':2},{'ros_namespace':'/uav2'},{'identity_verified':'true'}):
            with self.subTest(bad=bad):
                _,_,ack=self.connect(1,**bad);self.assertEqual(ack['type'],'hello_error')
        self.adapter.command_assign_task(1,{'mission_id':'still-correct'})
        self.assertEqual(receive_message(good,buf)['mission_id'],'still-correct')

    def test_same_device_reconnect_waits_for_manual_assignment(self):
        one,buf,_=self.connect(1);self.adapter.command_assign_task(1,{'mission_id':'resume'})
        receive_message(one,buf);one.shutdown(socket.SHUT_RDWR);one.close()
        self.assertTrue(wait_until(lambda:1 not in self.adapter.connected_uav_ids))
        two,buf,ack=self.connect(1)
        self.assertEqual(ack['type'],'hello_ack')
        two.settimeout(.15)
        with self.assertRaises(socket.timeout):
            receive_message(two,buf)
        self.adapter.command_assign_task(1,{'mission_id':'manual-latest'})
        two.settimeout(2)
        self.assertEqual(receive_message(two,buf)['mission_id'],'manual-latest')

    def test_rebound_device_uses_fixed_uav_mapping_without_device_handshake(self):
        one,buf,_=self.connect(1);self.adapter.command_assign_task(1,{'mission_id':'old-device'})
        receive_message(one,buf);one.shutdown(socket.SHUT_RDWR);one.close()
        self.assertTrue(wait_until(lambda:1 not in self.adapter.connected_uav_ids))
        self.config['vehicles'][0]['device_id']='new-device'
        two,buf,ack=self.connect(1,device_id='new-device');self.assertEqual(ack['type'],'hello_ack')
        two.settimeout(.15)
        with self.assertRaises(socket.timeout):
            receive_message(two,buf)

    def test_offline_unverified_assignment_is_not_cached(self):
        with self.assertRaises(RuntimeError):self.adapter.command_assign_task(1,{'mission_id':'unverified'})
        self.assertNotIn(1,self.adapter._latest_assignments)


class NavigationAndScanTest(unittest.TestCase):
    def test_geographic_points_use_gps_anchor_not_planning_origin(self):
        task={'coordinate_frame':'LOCAL_NORTH_WEST','waypoints_m':[[999,888]],'waypoints_wgs84':[[33.0,113.0]]}
        self.assertEqual(resolve_waypoints(task,[20,-40,5],[33,113,100],2000),[[20.,-40.]])
        east,north=wgs84_to_enu_xy(33.001,113.001,33,113)
        self.assertTrue(90<east<95);self.assertTrue(110<north<112)
        with self.assertRaises(ValueError):resolve_waypoints(task,None,None,2000)
        task['waypoints_wgs84']=[[34,114]]
        with self.assertRaises(ValueError):resolve_waypoints(task,[0,0,0],[33,113,100],2000)

    def test_scan_requires_matching_ack_and_minimum_hover(self):
        scan=ScanSession('new',10,30,0)
        scan.accept({'request_id':'old','state':'completed'});self.assertEqual(scan.state(15),'waiting')
        scan.accept({'request_id':'new','state':'completed'});self.assertEqual(scan.state(5),'waiting');self.assertEqual(scan.state(10),'completed')
        scan=ScanSession('next',10,30,0);self.assertEqual(scan.state(30),'timeout')
        scan.accept({'request_id':'next','state':'failed'});self.assertEqual(scan.state(31),'failed')

if __name__=='__main__':unittest.main()
