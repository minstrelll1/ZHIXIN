"""离线诊断回归：不连接飞机，不发送启动、解锁或飞行命令。"""
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from competition_backend.diagnostics import Diagnostics
from competition_backend.models import Telemetry
from competition_backend.program_logs import LatestProgramLog
from competition_backend.program_manager import ProgramManager
from competition_backend.tcp_adapter import TcpFleetAdapter
from competition_backend.distributed_adapter import DistributedFleetAdapter
from dataclasses import asdict


class DiagnosticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audit = Diagnostics(self.root/'sessions')
        self.addCleanup(self.audit.close)

    def rows(self, event):
        return [json.loads(line) for line in (self.audit.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()
                if json.loads(line)['event'] == event]

    def telemetry(self, valid=False):
        return Telemetry(uav_id=3, received_at=1234, connected=True, odom_valid=valid,
            gps_status=3 if valid else 1, gps_num=15, location_source=4,
            capabilities={'boot_id':'boot-A', 'boot_gps_home':None, 'localization_diagnostics':{
                'instance_id':'process-A', 'boot_home_reason':'gps_fix_below_3d',
                'alerts':[dict(sequence=1, source='PX4', severity=3, text='GPS fix too low', received_wall_time=456)]}})

    def test_invalid_then_recovery_records_clock_gps_and_home_without_repeating_alert(self):
        with patch('competition_backend.diagnostics.time.monotonic', return_value=100):
            for _ in range(20): self.audit.record_telemetry_diagnostics(self.telemetry())
            self.assertEqual(len(self.rows('定位与起降点诊断')),1)
            self.audit.record_telemetry_diagnostics(self.telemetry(True))
        rows=self.rows('定位与起降点诊断')
        self.assertEqual(len(rows),2)
        self.assertFalse(rows[0]['details']['state']['odom_valid'])
        self.assertTrue(rows[1]['details']['state']['odom_valid'])
        self.assertEqual(rows[0]['details']['measurements']['gps_num'],15)
        self.assertEqual(rows[0]['ground_monotonic'],100)
        self.assertEqual(len(self.rows('机载飞控原始告警')),1)

    def test_process_restart_can_restart_sequence_and_disk_failure_does_not_escape(self):
        item=self.telemetry(); self.audit.record_telemetry_diagnostics(item)
        item.capabilities['localization_diagnostics']['instance_id']='process-B'
        self.audit.record_telemetry_diagnostics(item)
        self.assertEqual(len(self.rows('机载飞控原始告警')),2)
        with patch.object(self.audit,'record',side_effect=OSError('disk full')):
            self.audit.record_telemetry_diagnostics(self.telemetry(True))

    def test_latest_log_archive_retains_previous_boot_and_survives_ground_restart(self):
        path=self.root/'uav3_onboard.log'
        def page(text,session,offset=0):
            raw=text.encode(); return dict(log_session=session,log_start=0,log_read_start=offset,
                offset=offset+len(raw),log=base64.b64encode(raw).decode())
        first=LatestProgramLog(path,'uav3',lambda value:value)
        first.consume(page('GPS invalid\n','session-A'),boot_id='boot-A')
        reopened=LatestProgramLog(path,'uav3',lambda value:value)
        reopened.consume(page('GPS valid\n','session-B'),boot_id='boot-B')
        archives=list((self.root/'history').rglob('*.log'))
        self.assertEqual(len(archives),1)
        self.assertIn('GPS invalid',archives[0].read_text(encoding='utf-8'))
        self.assertNotIn('GPS invalid',path.read_text(encoding='utf-8'))
        meta=json.loads(archives[0].with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['boot_id'],'boot-A')
        self.assertEqual(reopened.boot_id,'boot-B')
        reopened.consume({})
        self.assertEqual(len(list((self.root/'history').rglob('*.log'))),1)

    def test_failed_archive_does_not_destroy_previous_log(self):
        path=self.root/'uav3_onboard.log'
        mirror=LatestProgramLog(path,'uav3',lambda value:value)
        mirror.append_notice('previous evidence')
        before=path.read_bytes()
        with patch('competition_backend.program_logs.shutil.copyfile',side_effect=PermissionError('denied')):
            with self.assertRaises(OSError): mirror.consume(dict(log_session='new'))
        self.assertEqual(before,path.read_bytes())

    def test_request_classifies_ssh_auth_transport_and_remote_ros_separately(self):
        (self.root/'tools').mkdir()
        (self.root/'tools/remote_programs.py').write_text('def rpc(payload): pass',encoding='utf-8')
        manager=ProgramManager(self.root,{})
        manager.local=dict(uav_id=3,model='p600',onboard_host='192.168.1.212')
        manager.audit=self.audit
        cases=[('ssh: connect to host 192.168.1.212 port 22: Connection timed out','ssh_transport'),
               ('Permission denied (publickey,password).','ssh_authentication'),
               ('[ros_master] ROS Master unavailable; boot_id=A','ros_master')]
        for message,stage in cases:
            with patch('competition_backend.program_manager.ssh_identity_args',return_value=[]), patch(
                'competition_backend.program_manager.subprocess.run',return_value=SimpleNamespace(
                    returncode=255,stderr=message.encode(),stdout=b'')):
                with self.assertRaises(RuntimeError): manager._request('status',{})
            self.assertEqual(self.rows('机载管理请求诊断')[-1]['details']['stage'],stage)

    def test_diagnostic_payload_survives_air_to_ground_and_peer_forwarding(self):
        expected=self.telemetry().capabilities
        local=TcpFleetAdapter([3])
        local._handle_message(3,json.loads(json.dumps(dict(type='telemetry',uav_id=3,connected=True,
            position=[0,0,0],velocity=[0,0,0],capabilities=expected))))
        payload=asdict(local.telemetry_snapshot(3))
        peer=DistributedFleetAdapter([1,3],local_uav_id=1,node_id='ground-1',peers={3:'http://peer'},
            bind_host='127.0.0.1',port=0,uav_auth_token='',peer_token='')
        peer._request_json=lambda *args,**kwargs: {'telemetry':payload}
        peer._poll_peer_once(3,'http://peer')
        received=peer._peer_telemetry[3]
        self.assertEqual(received.capabilities,expected)
        self.audit.record_telemetry_diagnostics(received)
        self.assertEqual(self.rows('机载飞控原始告警')[0]['details']['alert']['text'],'GPS fix too low')

    def test_throttled_diagnostic_retains_source_time_and_never_crosses_boot(self):
        adapter=TcpFleetAdapter([3])
        caps=dict(self.telemetry().capabilities,diagnostics_supported=True)
        caps['localization_diagnostics']['sampled_monotonic']=123.
        def receive(value):
            adapter._handle_message(3,dict(type='telemetry',uav_id=3,connected=True,
                position=[0,0,0],velocity=[0,0,0],capabilities=value))
            return adapter.telemetry_snapshot(3).capabilities
        receive(caps)
        repeated=receive(dict(caps,localization_diagnostics=None))
        self.assertEqual(repeated['localization_diagnostics']['sampled_monotonic'],123.)
        reboot=receive(dict(caps,boot_id='boot-B',localization_diagnostics=None))
        self.assertIsNone(reboot.get('localization_diagnostics'))
        receive(caps)
        legacy=receive(dict(boot_id='boot-A'))
        self.assertNotIn('localization_diagnostics',legacy)


if __name__=='__main__': unittest.main()
