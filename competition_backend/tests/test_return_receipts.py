import unittest
import json
from types import SimpleNamespace
from competition_backend.distributed_adapter import DistributedFleetAdapter
from competition_backend.ros_adapter import RosFleetAdapter
from unittest.mock import Mock
from competition_backend.return_receipts import ReturnReceiptTracker
from competition_backend.tcp_adapter import TcpFleetAdapter
from competition_backend.models import Telemetry


class ReturnReceiptTest(unittest.TestCase):
    def setUp(self):
        self.now=100.
        self.audit=Mock()
        self.tracker=ReturnReceiptTracker(audit=self.audit,clock=lambda:self.now)
        self.sender=Mock()
        self.request_id=self.tracker.send(5,dict(mission_id='m',assignment_checksum='checksum',reason='manual'),self.sender)

    def ack(self, **extra):
        return dict(uav_id=5,request_id=self.request_id,mission_id='m',assignment_checksum='checksum',
                    state='accepted',ack_seq=1,detail='已接收',**extra)

    def see(self, ack=None):
        return self.tracker.snapshot({'5':{'return_ack':ack or {}}})['5']

    def test_network_write_is_not_receipt_and_timeout_never_resends(self):
        self.assertEqual(self.see()['state'],'waiting')
        self.now+=8
        self.assertEqual(self.see()['state'],'timeout')
        self.assertEqual(self.sender.call_count,1)
        self.assertTrue(self.see(self.ack())['ack_received'])
        self.assertEqual(self.see()['state'],'accepted')  # 回执已确认后失联仍保留证据。

    def test_each_identity_field_required_and_latest_sequence_wins(self):
        for key,value in [('uav_id',4),('mission_id','old'),('assignment_checksum','old'),('request_id','old')]:
            ack=self.ack();ack[key]=value
            self.assertEqual(self.see(ack)['state'],'waiting')
        ack=self.ack();ack.update(state='forwarded',ack_seq=2)
        self.assertEqual(self.see(ack)['state'],'forwarded')
        self.assertEqual(self.see(self.ack())['state'],'forwarded')

    def test_new_manual_retry_cannot_be_confirmed_by_previous_receipt(self):
        old=self.ack()
        new=self.tracker.send(5,dict(mission_id='m',assignment_checksum='checksum'),self.sender)
        self.assertNotEqual(new,self.request_id)
        self.assertEqual(self.see(old)['state'],'waiting')
        old.update(request_id=new,state='already_returning')
        self.assertEqual(self.see(old)['state'],'already_returning')

    def test_late_receipt_overrides_network_error_and_rejection_is_explicit(self):
        with self.assertRaises(TimeoutError):
            self.tracker.send(5,dict(mission_id='m',assignment_checksum='checksum'),Mock(side_effect=TimeoutError('offline')))
        record=self.see();self.assertEqual(record['state'],'send_failed')
        ack=self.ack();ack.update(request_id=record['request_id'],state='rejected',detail='遥控器已接管')
        view=self.see(ack)
        self.assertEqual(view['state'],'rejected')
        self.assertTrue(view['ack_received'])
        self.assertIn('遥控器',view['detail'])

    def test_per_aircraft_independent_and_old_generic_returning_not_an_ack(self):
        self.tracker.send(2,dict(mission_id='m',assignment_checksum='other'),self.sender)
        result=self.tracker.snapshot({'5':Telemetry(5,0,task_phase='returning'), '2':Telemetry(2,0)})
        self.assertEqual(result['5']['state'],'waiting')
        self.see(self.ack())
        self.assertEqual(self.tracker.snapshot({})['2']['state'],'waiting')

    def test_tcp_ack_and_reconnect_telemetry_reach_tracker(self):
        adapter=TcpFleetAdapter([5]);adapter._notify_telemetry=Mock()
        adapter._handle_message(5,dict(type='task_status',status=dict(state='return_ack',return_ack=self.ack())))
        snapshot=adapter.telemetry_snapshot(5)
        self.assertEqual(self.tracker.snapshot({'5':snapshot})['5']['state'],'accepted')
        reply=self.ack();reply.update(state='forwarded',ack_seq=2)
        adapter._handle_message(5,dict(type='telemetry',return_ack=reply,position=[0,0,1],velocity=[0,0,0]))
        self.assertEqual(self.tracker.snapshot({'5':adapter.telemetry_snapshot(5)})['5']['state'],'forwarded')

    def test_receipt_crosses_paired_ground_then_reaches_publisher(self):
        follower=DistributedFleetAdapter([1,5],local_uav_id=5,node_id='ground-5',peers={})
        publisher=DistributedFleetAdapter([1,5],local_uav_id=1,node_id='ground-1',peers={5:'http://peer'})
        follower.local_adapter._notify_telemetry=Mock()
        follower.local_adapter._handle_message(5,dict(type='task_status',status=dict(state='return_ack',return_ack=self.ack())))
        publisher._request_json=Mock(return_value={'telemetry':follower.local_telemetry_dict()})
        received=[];publisher.set_telemetry_sink(received.append)
        publisher._poll_peer_once(5,'http://peer')
        self.assertEqual(len(received),1)
        view=self.tracker.snapshot({5:received[0]})['5']
        self.assertTrue(view['ack_received'])
        self.assertEqual(view['request_id'],self.request_id)

    def test_ros_task_status_preserves_correlated_receipt(self):
        adapter=RosFleetAdapter([5]);received=[];adapter.set_telemetry_sink(received.append)
        adapter._task_callback(SimpleNamespace(data=json.dumps(dict(state='return_ack',return_ack=self.ack()))),5)
        self.assertEqual(self.tracker.snapshot({5:received[-1]})['5']['state'],'accepted')

    def test_malformed_receipt_cannot_break_status(self):
        ack=self.ack();ack['state']=[]
        self.assertEqual(self.see(ack)['state'],'waiting')

    def test_log_write_error_does_not_prevent_return_send(self):
        self.audit.record.side_effect=OSError('disk busy')
        self.tracker.send(2,dict(mission_id='m',assignment_checksum='c'),self.sender)
        self.assertEqual(self.sender.call_count,2)


if __name__=='__main__':unittest.main()
