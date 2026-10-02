import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from test_executor_autonomy import AutonomyTest, module
from su17_competition_executor.task_protocol import assignment_checksum

class RecognitionPublisherTest(unittest.TestCase):
    def node(self,directory):
        node=AutonomyTest().executor(directory)
        assignment=copy.deepcopy(node._assignment)
        assignment['subject']='subject1'
        assignment['task']['recognition_selection']={'category_count':3,'category_ids':[0,7,15]}
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
                self.assertEqual(events[:2],[('ros',[0,7,15]),('status','task_received')])
                node._accept_assignment(msg)
                self.assertEqual(node.recognition_categories_pub.publish.call_count,1)

    def test_active_task_and_bad_checksum_still_rejected(self):
        for reason in ['checksum','active']:
            with self.subTest(reason=reason),tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory)
                if reason=='checksum':msg['task']['recognition_selection']['category_ids']=[18]
                if reason=='active':node._motion_thread=Mock(is_alive=Mock(return_value=True))
                node._accept_assignment(msg)
                self.assertFalse(node._assignment_acked)
                self.assertEqual(node._publish_status.call_args.args[0],'task_rejected')
                node.recognition_categories_pub.publish.assert_not_called()

    def test_program_b_route_speed_does_not_block_assignment_or_takeoff(self):
        for mode in ('internal', 'external'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                node, msg = self.node(directory)
                node.max_speed = 1.0
                node.flight_speed_limit = 1.0
                node.search_speed = 0.3
                node.return_speed = 0.3
                node.identity = {'uav_id': 3}
                node._refresh_flight_speed_limit = Mock()
                msg['controller_mode'] = mode
                msg['task']['speed_mps'] = 5.0
                msg['assignment_checksum'] = assignment_checksum(msg)
                node._accept_assignment(msg)
                self.assertEqual(node._assignment_acked, mode == 'external')
                if mode == 'external':
                    # 故意给无效起飞高度，使测试停在限速检查之后，不实际启动飞行。
                    self.assertFalse(node._run_takeoff({'target_altitude_m': -1.0}))
                    self.assertIn('起飞高度', node._publish_status.call_args.kwargs['error'])
                else:
                    self.assertIn('规划航速', node._publish_status.call_args.kwargs['error'])

    def test_zero_categories_and_publish_error_do_not_reject_task(self):
        for failure in [False, True]:
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory)
                msg['task']['recognition_selection']={'category_count':0,'category_ids':[]}
                msg['assignment_checksum']=assignment_checksum(msg)
                if failure:node.recognition_categories_pub.publish.side_effect=RuntimeError('ROS disconnected')
                node._accept_assignment(msg)
                self.assertTrue(node._assignment_acked)
                self.assertEqual(node._publish_status.call_args.args[0],'task_received')
                self.assertEqual(node.recognition_categories_pub.publish.call_args.args[0].data,[])

    def test_restart_resets_stale_selection_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            node,msg=self.node(directory)
            node._assignment=msg;node._assignment_acked=True
            node._restore_recognition_categories()
            self.assertEqual(node.recognition_categories_pub.publish.call_args.args[0].data,[])
            node._assignment_acked=False
            node._restore_recognition_categories()
            self.assertEqual(node.recognition_categories_pub.publish.call_count,2)

    def test_legacy_subject_one_clears_categories_other_subject_does_not_publish(self):
        for legacy in [True,False]:
            with tempfile.TemporaryDirectory() as directory:
                node,msg=self.node(directory)
                if legacy:msg['task'].pop('recognition_selection')
                else:msg['subject']='subject2'
                msg['assignment_checksum']=assignment_checksum(msg);node._accept_assignment(msg)
                self.assertTrue(node._assignment_acked)
                if legacy:self.assertEqual(node.recognition_categories_pub.publish.call_args.args[0].data,[])
                else:node.recognition_categories_pub.publish.assert_not_called()

    def test_competition_time_follows_assignment_and_ticks_from_monotonic_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            node, msg = self.node(directory)
            msg['competition_time'] = {
                'schema_version': 1, 'mission_id': msg['mission_id'],
                'running': True, 'synchronized': True, 'elapsed_seconds': 25.0,
                'session_id': 'session-1', 'revision': 2,
                'publisher_terminal_id': 1, 'updated_by': 1,
            }
            msg['assignment_checksum'] = assignment_checksum(msg)
            with patch.object(module.time, 'monotonic', return_value=100.0):
                node._accept_assignment(msg)
            self.assertTrue(node._assignment_acked)
            first = json.loads(node.competition_time_pub.publish.call_args.args[0].data)
            self.assertEqual(first['elapsed_seconds'], 25.0)
            self.assertEqual(first['session_id'], 'session-1')
            with patch.object(module.time, 'monotonic', return_value=104.25):
                node._publish_competition_time()
            later = json.loads(node.competition_time_pub.publish.call_args.args[0].data)
            self.assertEqual(later['elapsed_seconds'], 29.25)
            self.assertEqual(later['mission_id'], msg['mission_id'])

    def test_malformed_competition_time_rejects_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            node, msg = self.node(directory)
            msg['competition_time'] = {'running': True, 'elapsed_seconds': -1}
            msg['assignment_checksum'] = assignment_checksum(msg)
            node._accept_assignment(msg)
            self.assertFalse(node._assignment_acked)
            node.competition_time_pub.publish.assert_not_called()

if __name__=='__main__':unittest.main()
