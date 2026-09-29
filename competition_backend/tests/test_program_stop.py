import asyncio
import contextlib
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from competition_backend.ground_entry import GroundEntry
from competition_backend.program_manager import ProgramManager
from competition_shared.fleet import default_fleet
from test_program_manager import remote


class ProgramStopTests(unittest.TestCase):
    def test_local_only_api_routes_stop_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'fleet.json'
            config.write_text(json.dumps(default_fleet()),encoding='utf-8')
            manager=ProgramManager(tmp,{})
            manager.stop_program=Mock(return_value=dict(state='stopped',stop_verified=True))
            entry=GroundEntry({'COMPETITION_FLEET_CONFIG':str(config)},programs_factory=lambda *args:manager)
            with TestClient(entry) as client:
                self.assertEqual(client.post('/api/v1/programs/flight/stop',headers={'Origin':'https://evil.example'}).status_code,403)
                manager.stop_program.assert_not_called()
                self.assertTrue(client.post('/api/v1/programs/flight/stop').json()['stop_verified'])
                manager.stop_program.assert_called_once_with('flight')
                manager.stop_program.side_effect=RuntimeError('SSH 断开，停止未确认')
                self.assertEqual(client.post('/api/v1/programs/detection/stop').status_code,409)
                self.assertEqual(client.post('/api/v1/programs/ground/stop').status_code,409)
                entry.shutdown_callback=Mock()
                with patch('competition_backend.ground_entry.subprocess.Popen') as spawn, patch('competition_backend.ground_entry.asyncio', wraps=asyncio) as module:
                    spawn.return_value.stdout.readline.return_value=b"GROUND_STOP_READY\n"
                    loop=module.get_running_loop
                    loop.return_value=Mock()
                    self.assertEqual(client.post('/api/v1/programs/ground/stop').json()['state'],'stopping')
                    self.assertEqual(client.post('/api/v1/programs/ground/stop').status_code,200)
                    loop.return_value.call_later.assert_called_once_with(.5,entry.shutdown_callback)
                    self.assertLessEqual(spawn.call_count,1)

    def test_manual_stop_verified_failure_and_no_automatic_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProgramManager(tmp,{})
            manager.local=dict(uav_id=1,model='p600',onboard_host='192.168.1.202')
            manager._request=Mock(return_value={'flight':dict(state='stopped',stop_verified=True,detail='已退出')})
            self.assertTrue(manager.stop_program('flight')['stop_verified'])
            manager._request.assert_called_once_with('stop',{},program='flight')
            manager._request=Mock(side_effect=TimeoutError('SSH timeout'))
            with self.assertRaisesRegex(RuntimeError,'停止未确认'):
                manager.stop_program('detection')
            self.assertFalse(manager.snapshot()['programs']['detection']['stop_verified'])
            calls=[]
            def request(action,offsets,program=None):
                calls.append((action,program))
                if program=='onboard': manager.stop_event.set()
                return {key:dict(state='stopped') for key in remote.NAMES}
            manager._request=request
            manager._watch()
            self.assertEqual(calls,[('status',None),('start','onboard')])

    def test_tree_excludes_shared_ros_and_reused_pids(self):
        def item(pid,parent,args,stamp='one'):
            return dict(pid=pid,parent=parent,args=args,stamp=stamp)
        table={1:item(1,0,['roslaunch']),2:item(2,1,['node']),3:item(3,1,['/opt/ros/bin/rosmaster']),
               4:item(4,3,['shared']),5:item(5,0,['unrelated'],stamp='new')}
        tree=remote.collect_tree(table,{1:table[1],5:dict(stamp='old')})
        self.assertEqual(set(tree),{1,2})

    def test_stop_escalates_only_owned_tree_and_verifies_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            table={120:dict(pid=120,parent=0,args=['roslaunch'],stamp='a'),
                   121:dict(pid=121,parent=120,args=['node'],stamp='b'),
                   122:dict(pid=122,parent=0,args=['rosout'],stamp='c'),
                   150:dict(pid=150,parent=0,args=['other_program'],stamp='d')}
            remote.write_json(directory/'process.json',table[120])
            live=set(table)
            killed=[]
            clock=[0]
            def now():
                clock[0]+=2
                return clock[0]
            def kill(pid,sig):
                killed.append((pid,sig))
                if sig==signal.SIGKILL: live.discard(pid)
            health={'flight':dict(present=[],pids={})}
            with patch.object(remote,'existing',return_value={}), patch.object(remote,'process_table',side_effect=lambda:{pid:item for pid,item in table.items() if pid in live}), patch.object(remote,'alive',side_effect=lambda item:item.get('pid') in live), patch.object(remote,'ros_health',return_value=health), patch.object(signal,'SIGKILL',9,create=True),patch.object(remote.os,'kill',side_effect=kill), patch.object(remote.time,'monotonic',side_effect=now),patch.object(remote.time,'sleep'):
                result=remote.stop_program(directory,'flight',1)
            self.assertTrue(result['stop_verified'])
            self.assertEqual({pid for pid,sig in killed},{120,121})
            self.assertEqual(live,{122,150})
            self.assertIn((120,signal.SIGINT),killed)
            self.assertIn((120,9),killed)
            self.assertTrue(remote.read_json(directory/'process.json')['stopped_by_operator'])

    def test_launcher_owning_ros_master_is_not_terminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)
            table={120:dict(pid=120,parent=0,args=['roslaunch'],stamp='a'),
                   121:dict(pid=121,parent=120,args=['rosmaster'],stamp='b')}
            remote.write_json(directory/'process.json',table[120])
            with patch.object(remote,'existing',return_value={}),patch.object(remote,'process_table',return_value=table),patch.object(remote,'alive',side_effect=lambda item:item.get('pid') in table),patch.object(remote,'ros_health',return_value={'flight':dict(present=[],pids={})}),patch.object(remote.os,'kill') as kill:
                with self.assertRaisesRegex(RuntimeError,'共享 ROS'):
                    remote.stop_program(directory,'flight',1)
                kill.assert_not_called()

    def test_ros_nodes_still_present_cannot_report_stopped(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(remote,'existing',return_value={}), patch.object(remote,'process_table',return_value={}),patch.object(remote,'ros_health',return_value={'flight':dict(present=['/uav1/trajectory_follower'],pids={})}),patch.object(signal,'SIGKILL',9,create=True):
            result=remote.stop_program(Path(tmp),'flight',1)
            self.assertFalse(result['stop_verified'])
            self.assertEqual(result['state'],'error')

    def test_stop_rpc_never_starts_other_programs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);root=home/'competition_development'
            (root/'tools').mkdir(parents=True);(root/'tools/start_onboard_stack.sh').touch()
            with patch.dict('sys.modules',{'fcntl':SimpleNamespace(flock=lambda *args:None,LOCK_EX=2)}),patch.object(remote.Path,'home',return_value=home),patch.object(remote,'ros_health',return_value={key:dict(ready=False,present=[],missing=[]) for key in remote.NAMES}),patch.object(remote,'existing',return_value={}),patch.object(remote,'stop_program',return_value=dict(state='stopped',stop_verified=True)) as stop,patch.object(remote.subprocess,'Popen') as spawn,contextlib.redirect_stdout(io.StringIO()):
                remote.rpc(dict(action='stop',program='flight',uav_id=1,model='p600'))
                spawn.assert_not_called()
                self.assertEqual(stop.call_args.args[1:],('flight',1))

if __name__=='__main__': unittest.main()
