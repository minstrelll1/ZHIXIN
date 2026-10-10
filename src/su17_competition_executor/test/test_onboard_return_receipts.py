import tempfile
import threading
import unittest
from unittest.mock import Mock
import test_executor_autonomy as existing


class OnboardReturnReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.node=existing.AutonomyTest().executor(self.tmp.name)
        self.node.enable_motion=True
        self.node.external_return_pub=Mock()
        self.node.external_return_topic='/ground_mission_planner/vehicle_3/return_home'
        self.node._assignment['controller_mode']='external'
        self.node._run_return=existing.module.OnboardTaskExecutor._run_return.__get__(self.node)
        self.payload=dict(request_id='a'*32,mission_id='test',assignment_checksum=self.node._assignment['assignment_checksum'],reason='manual')

    def wait(self):
        if self.node._motion_thread: self.node._motion_thread.join(2)

    def test_receipt_published_then_transferred_and_manual_retry_does_not_repeat_B(self):
        self.node._start_return(self.payload);self.wait()
        ack=self.node._return_ack_snapshot()
        self.assertEqual(ack['state'],'forwarded')
        self.assertEqual(ack['request_id'],self.payload['request_id'])
        self.assertEqual(ack['ack_seq'],2)
        self.node.external_return_pub.publish.assert_called_once()
        receipts=[c.kwargs['return_ack']['state'] for c in self.node._publish_status.call_args_list if c.args[0]=='return_ack']
        self.assertEqual(receipts,['accepted','forwarded'])
        self.node._start_return(self.payload);self.wait()
        self.node.external_return_pub.publish.assert_called_once()
        self.node._start_return(dict(self.payload,request_id='b'*32));self.wait()
        self.assertEqual(self.node._return_ack_snapshot()['state'],'already_returning')
        self.assertEqual(self.node._return_ack_snapshot()['request_id'],'b'*32)
        self.node.external_return_pub.publish.assert_called_once()

    def test_manual_retry_during_preparation_gets_final_failure_without_duplicate_motion(self):
        began=threading.Event();release=threading.Event()
        def publish(_):
            began.set();release.wait(2)
            raise RuntimeError('ROS publish failed')
        self.node.external_return_pub.publish.side_effect=publish
        try:
            self.node._start_return(self.payload)
            self.assertTrue(began.wait(1))
            self.node._start_return(dict(self.payload,request_id='b'*32))
            self.assertEqual(self.node._return_ack_snapshot()['state'],'accepted')
        finally:
            release.set();self.wait()
        ack=self.node._return_ack_snapshot()
        self.assertEqual(ack['request_id'],'b'*32)
        self.assertEqual(ack['state'],'failed')
        self.node.external_return_pub.publish.assert_called_once()

    def test_rejected_requests_have_receipt_but_no_motion(self):
        for modification in ('manual','disabled','mission','checksum'):
            with self.subTest(case=modification):
                self.node._manual_override=modification=='manual'
                self.node.enable_motion=modification!='disabled'
                payload=dict(self.payload,request_id=modification)
                if modification=='mission':payload['mission_id']='old'
                if modification=='checksum':payload['assignment_checksum']='old'
                self.node._start_return(payload)
                self.assertEqual(self.node._return_ack_snapshot()['state'],'rejected')
                self.node.external_return_pub.publish.assert_not_called()
                self.assertIsNone(self.node._motion_thread)

    def test_internal_failure_has_negative_receipt_and_can_be_manually_retried(self):
        self.node._assignment['controller_mode']='internal'
        self.node._fly_to.return_value=(False,'local telemetry stale')
        self.node._start_return(self.payload);self.wait()
        self.assertEqual(self.node._return_ack_snapshot()['state'],'failed')
        self.node._start_return(dict(self.payload,request_id='b'*32));self.wait()
        self.assertEqual(self.node._fly_to.call_count,2)
        self.node._land.assert_not_called()

    def test_receipt_persists_same_boot_but_not_after_reboot(self):
        self.node._start_return(self.payload);self.wait()
        self.node._last_return_ack={}
        self.assertEqual(self.node._return_ack_snapshot()['state'],'forwarded')
        self.node._boot_id='new-boot'
        self.assertEqual(self.node._return_ack_snapshot(),{})

    def test_old_ground_command_without_request_id_still_returns(self):
        self.node._start_return(dict(mission_id='test',reason='manual'));self.wait()
        self.node.external_return_pub.publish.assert_called_once()
        self.assertEqual(self.node._return_ack_snapshot(),{})

    def test_exceptions_are_reported_and_never_mislabelled_successful_return(self):
        self.node.external_return_pub.publish.side_effect=RuntimeError('ROS publish failed')
        self.node._start_return(self.payload);self.wait()
        self.assertEqual(self.node._return_ack_snapshot()['state'],'failed')
        self.assertNotIn('successful_return',self.node._progress)


if __name__=='__main__':unittest.main()
