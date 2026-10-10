"""本机回环TCP验证：不连接飞机、不发送任何飞行动作、不调整系统时间。"""
import copy
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/su17_competition_executor/src'))
from su17_competition_executor.tcp_link import OnboardTcpLink
from competition_backend.tcp_adapter import TcpFleetAdapter, _ClientConnection
from competition_backend.time_reference import TimeReferenceCollector
from competition_shared.target_time import map_localization_time
from test_tcp_adapter import receive_message, send_message, wait_until

BOOT = 'fa63bc01-57fc-49d6-9824-f65e4350b249'


class ClockProbeLinkTest(unittest.TestCase):
    def setUp(self):
        self.adapter=TcpFleetAdapter([5], bind_host='127.0.0.1', port=0, auth_token='test-only')
        self.adapter.start()
        self.addCleanup(self.adapter.stop)

    def test_live_tcp_probe_recovers_thirteen_pending_records_without_ssh_or_motion(self):
        handler=Mock()
        original_read = Path.read_text
        def read_path(path, *args, **kwargs):
            return BOOT if str(path).replace(chr(92), '/') == '/proc/sys/kernel/random/boot_id' else original_read(path, *args, **kwargs)
        with patch('su17_competition_executor.tcp_link.Path.read_text', autospec=True, side_effect=read_path):
            link=OnboardTcpLink(5,'127.0.0.1',self.adapter.bound_port,'test-only',handler)
            try:
                link.start()
                self.assertTrue(wait_until(lambda: link.connected))
                before=self.adapter.telemetry_snapshot(5).received_at
                sample=self.adapter.clock_probe(5)
                self.assertEqual(sample['boot_id'],BOOT)
                self.assertEqual(sample['transport'],'task_tcp')
                self.assertEqual(self.adapter.telemetry_snapshot(5).received_at,before)
                self.assertLess(abs(sample['remote_monotonic']-time.monotonic()),1.)
                # 与故障同类：13条含localization_time/开机证据但缺参考的记录。
                records=[]
                ros_ns=1791617100000000000
                sample_ns=time.monotonic_ns()
                for i in range(13):
                    age_ns=(i+1)*1000000000
                    stamp=ros_ns-age_ns
                    records.append(dict(uav_id=5,localization_time=dict(secs=stamp//10**9,nsecs=stamp%10**9),
                        localization_clock=dict(schema_version=1,boot_id=BOOT,segment_id=1,
                            mapping_valid=True,use_sim_time=False,sample_monotonic_ns=sample_ns,
                            event_monotonic_ns=sample_ns-age_ns,source_ros_ns=ros_ns,
                            segment_start_monotonic_ns=sample_ns-30000000000,sampling_uncertainty_ns=1000)))
                originals=copy.deepcopy(records)
                self.assertTrue(all(map_localization_time(r,5,{})[1]['reason']=='missing_boot_reference' for r in records))
                with tempfile.TemporaryDirectory() as tmp:
                    updated=Mock()
                    collector=TimeReferenceCollector(Path(tmp)/'time_references.json',1,lambda:True,
                        lambda:[5],self.adapter.clock_probe,on_update=updated)
                    collector.collect_once()
                    refs=json.loads(collector.path.read_text())['references']
                    results=[map_localization_time(r,5,refs) for r in records]
                    self.assertEqual(sum(a['status']=='corrected' for _,a in results),13)
                    self.assertEqual(updated.call_count,1)
                    self.assertEqual(refs[BOOT]['transport'],'task_tcp')
                    self.assertEqual(records,originals)
                    for i,(value,audit) in enumerate(results):
                        self.assertAlmostEqual(value['localization_time']['secs'],time.time()-(i+1),delta=2.)
                    # 其他开机参考不能修复该批记录。
                    wrong={BOOT+'different':refs[BOOT]}
                    self.assertEqual(map_localization_time(records[0],5,wrong)[1]['reason'],'missing_boot_reference')
                handler.assert_not_called()
            finally:
                link.stop()

    def connect(self, capability=1):
        client=socket.create_connection(('127.0.0.1',self.adapter.bound_port),timeout=2)
        self.addCleanup(client.close)
        buf=bytearray()
        send_message(client,dict(type='hello',protocol_version=1,uav_id=5,auth_token='test-only',clock_probe_version=capability))
        receive_message(client,buf)
        return client,buf

    def test_old_nodes_receive_no_unknown_command(self):
        client,buf=self.connect(capability=None)
        with self.assertRaises(NotImplementedError): self.adapter.clock_probe(5)
        client.settimeout(.15)
        with self.assertRaises(socket.timeout): client.recv(1)

    def test_stale_correlation_ignored_timeout_cleaned_and_disconnect_wakes_waiter(self):
        client,buf=self.connect()
        result=[]
        def probe():
            try: result.append(self.adapter.clock_probe(5,timeout=.2))
            except Exception as e: result.append(e)
        worker=threading.Thread(target=probe);worker.start()
        request=receive_message(client,buf)
        send_message(client,dict(type='clock_probe_reply',uav_id=5,request_id='stale',boot_id=BOOT,remote_monotonic=1,remote_wall=2))
        worker.join(2)
        self.assertIsInstance(result.pop(),TimeoutError)
        self.assertFalse(self.adapter._clock_probes)
        worker=threading.Thread(target=probe);worker.start()
        new_request=receive_message(client,buf)
        self.assertNotEqual(new_request['request_id'],request['request_id'])
        send_message(client,dict(type='clock_probe_reply',uav_id=5,request_id=request['request_id'],boot_id=BOOT,remote_monotonic=1,remote_wall=2))
        client.shutdown(socket.SHUT_RDWR);client.close()
        worker.join(2)
        self.assertIsInstance(result.pop(),RuntimeError)
        self.assertFalse(self.adapter._clock_probes)

    def test_probe_skips_busy_send_queue_without_closing_flight_link(self):
        sock=Mock()
        client=_ClientConnection(sock,('127.0.0.1',1))
        client.send_lock.acquire()
        try:
            with self.assertRaises(TimeoutError): client.send(dict(type='clock_probe'),probe=True)
            sock.close.assert_not_called()
            sock.sendall.assert_not_called()
        finally:
            client.send_lock.release()


if __name__=='__main__': unittest.main()
