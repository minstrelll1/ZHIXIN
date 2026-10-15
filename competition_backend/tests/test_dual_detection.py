"""两个检测程序的独立配置、生命周期、进程隔离与界面接入；无真实 SSH/飞行操作。"""
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from competition_backend.program_manager import NAMES, ProgramManager
from test_program_manager import remote, ROOT
import test_program_boot as boot_tests


class DualDetectionLifecycleTests(unittest.TestCase):
    setUp = boot_tests.RemoteBootTests.setUp
    spawn = boot_tests.RemoteBootTests.spawn
    rpc = boot_tests.RemoteBootTests.rpc

    def test_both_start_once_at_boot_and_reconnect_never_restarts(self):
        result = self.rpc('auto_start')
        self.assertEqual(set(result)-{'_lifecycle'}, set(remote.NAMES))
        by_key = {Path(call[3]).name: call[-1] for call in self.calls}
        self.assertIn('uav_yolo26_vb_geolocation.launch', by_key['detection'])
        self.assertIn('uav_yolo26_ren_geolocation.launch', by_key['detection_person'])
        self.live.clear()
        self.rpc('status'); self.rpc('auto_start')
        self.assertEqual(len(self.calls), 5)

    def test_person_failure_does_not_block_vehicle_or_other_programs(self):
        result = self.rpc('auto_start', commands={'detection_person': ''})
        self.assertEqual(result['detection_person']['state'], 'error')
        self.assertEqual({Path(call[3]).name for call in self.calls}, set(remote.NAMES)-{'detection_person'})
        self.rpc('auto_start')
        self.assertEqual(len(self.calls), 4)

    def test_manual_start_and_stop_are_independent_and_read_new_command(self):
        for key in ('detection', 'detection_person'):
            command = 'echo config_test_' + key + '_{uav_id}'
            count = len(self.calls)
            self.rpc('start', program=key, commands={key:command})
            self.assertEqual(len(self.calls), count+1)
            self.assertEqual(self.calls[-1][-1], command.replace('{uav_id}','2'))
            self.rpc('start', program=key, commands={key:'echo new_config'})
            self.assertEqual(len(self.calls), count+1)
            def stop(directory, name, uid):
                self.assertEqual(name, key)
                for filename in ('process.json','worker.json'):
                    meta = remote.read_json(directory/filename)
                    self.live.pop(meta.get('pid'), None)
                    remote.write_json(directory/filename, {})
                return dict(state='stopped', stop_verified=True)
            with patch.object(remote,'stop_program',side_effect=stop):
                self.rpc('stop', program=key)
            self.rpc('auto_start')
            self.assertEqual(len(self.calls), count+1)
            self.rpc('start', program=key, commands={key:'echo new_config'})
            self.assertEqual(self.calls[-1][-1], 'echo new_config')
            self.assertEqual(Path(self.calls[-1][3]).name, key)

    def test_upgrade_of_existing_boot_does_not_auto_start_added_detector(self):
        runtime = self.root/'ground_runtime/programs_uav2'
        runtime.mkdir(parents=True)
        remote.write_json(runtime/'boot_autostart.json', dict(boot_id=self.boot, reason='auto_start'))
        self.assertEqual(self.rpc()['_lifecycle']['auto_start_pending'], [])
        self.rpc('auto_start')
        self.assertEqual(self.calls, [])
        self.rpc('start', program='detection_person')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(Path(self.calls[0][3]).name,'detection_person')


def process(pid, parent, args):
    return dict(pid=pid, parent=parent, stamp='s', args=args)


class DualDetectionIntegrationTests(unittest.TestCase):
    def table(self):
        return {
            100: process(100,0,['roslaunch','spirecv_ros','uav_yolo26_vb_geolocation.launch','uav_id:=2']),
            101: process(101,100,['/opt/detector_shared_binary','__name:=vehicles','__ns:=/uav2']),
            200: process(200,0,['roslaunch','spirecv_ros','uav_yolo26_ren_geolocation.launch','uav_id:=2']),
            201: process(201,200,['/opt/detector_shared_binary','__name:=people','__ns:=/uav2']),
            300: process(300,0,['roslaunch','flight.launch','uav_id:=2']),
            400: process(400,0,['roslaunch','spirecv_ros','uav_yolo26_ren_geolocation.launch','uav_id:=3']),
        }

    def test_configuration_exact_commands_for_all_six_uavs(self):
        config=json.loads((ROOT/'config/onboard_programs.json').read_text(encoding='utf-8-sig'))
        self.assertEqual(set(config['commands']),set(remote.NAMES))
        self.assertEqual(set(NAMES)-{'ground'},set(remote.NAMES))
        for key,launch in [('detection','uav_yolo26_vb_geolocation.launch'),('detection_person','uav_yolo26_ren_geolocation.launch')]:
            for uid in range(1,7):
                expected = ('source ~/SpireCV_bj/src/spirecv-ros/devel/setup.bash\n'
                    'roslaunch spirecv_ros %s uav_id:=%d runtime_mode:=debug '
                    'debug_save_dir:=/home/amov/yolo_debug debug_save_dir_all:=/home/amov/yolo_debug_all '
                    'camera_params:=/home/amov/SpireCV_bj/src/spirecv-ros/sv-rosapp/camera_params/calib_gx40_720.yaml '
                    'detection_area_filter_enabled:=true external_debug_save_enabled:=true') % (launch,uid)
                self.assertEqual(remote.command_for(key,uid,'p600',config['commands']),expected)
                self.assertEqual(remote.command_for(key,uid,'p600'),expected)

    def test_actual_ros_names_come_from_each_process_tree(self):
        table=self.table()
        with tempfile.TemporaryDirectory() as tmp, patch.object(remote.Path,'home',return_value=Path(tmp)), patch.object(remote,'process_table',return_value=table):
            result=remote.expected_nodes(2)
        self.assertEqual(result['detection'],['/uav2/vehicles'])
        self.assertEqual(result['detection_person'],['/uav2/people'])
        self.assertEqual(set(remote.matching_launchers(table,'detection',2)),{100})
        self.assertEqual(set(remote.matching_launchers(table,'detection_person',2)),{200})

    def test_stopping_one_detector_keeps_other_detector_and_flight_alive(self):
        for key, selected in [('detection',{100,101}),('detection_person',{200,201})]:
            with self.subTest(program=key), tempfile.TemporaryDirectory() as tmp:
                table=self.table(); live=set(table); killed=[]
                def kill(pid,sig):
                    killed.append(pid)
                    live.discard(pid)
                    for child,item in table.items():
                        if item['parent']==pid:live.discard(child)
                health={name:dict(present=[],pids={}) for name in remote.NAMES}
                with patch.object(remote,'process_table',side_effect=lambda:{pid:item for pid,item in table.items() if pid in live}), patch.object(remote,'alive',side_effect=lambda item:item.get('pid') in live), patch.object(remote,'ros_health',return_value=health), patch.object(remote.os,'kill',side_effect=kill), patch.object(signal,'SIGKILL',9,create=True):
                    result=remote.stop_program(Path(tmp),key,2)
                self.assertTrue(result['stop_verified'])
                self.assertEqual(live,set(table)-selected)
                self.assertTrue(set(killed)<=selected)
                self.assertEqual(set(result['checked_pids']),selected)

    def test_two_status_lights_and_shared_console_controls_are_present(self):
        html=(ROOT/'competition_backend/competition_backend/web/index.html').read_text(encoding='utf-8-sig')
        self.assertIn('data-program="detection"><i></i>车辆工事检测程序',html)
        self.assertIn('data-program="detection_person"><i></i>人员检测程序',html)
        self.assertNotIn('><i></i>目标检测程序</button>',html)
        for name in ('startProgram','stopProgram','programLogSearch','programLogMore'):
            self.assertIn('id="'+name+'"',html)
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProgramManager(tmp,{})
            manager.local=dict(uav_id=2,model='p600')
            self.assertNotEqual(manager._path('detection'),manager._path('detection_person'))


if __name__=='__main__': unittest.main()
