import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from test_executor_autonomy import AutonomyTest, module
from su17_competition_executor.task_protocol import assignment_checksum

class RecognitionPublisherTest(unittest.TestCase):
    def node(self,directory):
        node=AutonomyTest().executor(directory)
        assignment=copy.deepcopy(node._assignment)
        assignment['subject']='subject1'
        assignment['task']['recognition_selection']={'category_count':3,'category_ids':[1,8,12]}
        assignment['assignment_checksum']=assignment_checksum(assignment)
        node._assignment=None
        node.max_distance_from_home=10
        node.recognition_categories_pub=Mock()
        node.recognition_categories_topic='/uav3/competition/recognition_categories'
        node.log_full_task=False
        node._checkpoint=Mock()
        return node,assignment

    def test_accept_publishes_before_ack_in_both_control_modes(self):
        for mode in ['internal','external']:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory);msg['controller_mode']=mode;msg['assignment_checksum']=assignment_checksum(msg)
                events=[]
                node.recognition_categories_pub.publish.side_effect=lambda m:events.append(('ros',m.data))
                node._publish_status.side_effect=lambda kind,**kw:events.append(('status',kind))
                node._accept_assignment(msg)
                self.assertTrue(node._assignment_acked)
                self.assertEqual(events[:2],[('ros',[1,8,12]),('status','task_received')])
                node._accept_assignment(msg)
                self.assertEqual(node.recognition_categories_pub.publish.call_count,1)

    def test_rejected_active_and_failed_publish_never_ack(self):
        for reason in ['invalid','active','publish_error']:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory)
                if reason=='invalid':msg['task']['recognition_selection']['category_ids']=[16]
                if reason=='active':node._motion_thread=Mock(is_alive=Mock(return_value=True))
                if reason=='publish_error':node.recognition_categories_pub.publish.side_effect=RuntimeError('ROS disconnected')
                node._accept_assignment(msg)
                self.assertFalse(node._assignment_acked)
                self.assertEqual(node._publish_status.call_args.args[0],'task_rejected')
                if reason!='publish_error':node.recognition_categories_pub.publish.assert_not_called()

    def test_restart_restores_only_verified_local_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            node,msg=self.node(directory)
            node._assignment=msg;node._assignment_acked=True
            node._restore_recognition_categories()
            self.assertEqual(node.recognition_categories_pub.publish.call_args.args[0].data,[1,8,12])
            node._assignment_acked=False
            node._restore_recognition_categories()
            self.assertEqual(node.recognition_categories_pub.publish.call_count,1)

    def test_legacy_and_other_subjects_do_not_publish(self):
        for legacy in [True,False]:
            with tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory)
                if legacy:msg['task'].pop('recognition_selection')
                else:msg['subject']='subject2'
                msg['assignment_checksum']=assignment_checksum(msg);node._accept_assignment(msg)
                self.assertTrue(node._assignment_acked)
                node.recognition_categories_pub.publish.assert_not_called()

if __name__=='__main__':unittest.main()
