"""串联程序 B 只读事件、各机补传门闩和发布端六机上报计划，不连接飞机/赛方。"""
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'competition_backend'))
sys.path.insert(0, str(ROOT / 'src' / 'su17_image_transfer' / 'test'))
from competition_backend.return_report_schedule import ReturnReportSchedule
import test_final_reconciliation as image_tests
import test_successful_return as bridge_tests
from test_executor_autonomy import assignment_checksum


class ReturnPipelineTest(unittest.TestCase):
    def test_six_real_bridge_events_feed_each_image_gate_and_one_reporting_plan(self):
        plan = ReturnReportSchedule()
        checksums = {}
        for uid in range(1, 7):
            case = bridge_tests.SuccessfulReturnTest()
            case.setUp()
            try:
                case.arm_observer()
                node = case.node
                node.uav_id = uid
                node.external_follower_node = '/uav{}/trajectory_follower'.format(uid)
                node._assignment['uav_id'] = uid
                checksum = assignment_checksum(node._assignment)
                node._assignment['assignment_checksum'] = checksum
                node._progress['external_return_watch']['assignment_checksum'] = checksum
                checksums[str(uid)] = checksum
                node._external_return_command_callback(case.land_command(caller=node.external_follower_node))
                self.assertFalse(node._successful_return_snapshot())
                node._control_callback(types.SimpleNamespace(uav_id=uid, control_state=3, failsafe=False))
                wire = node.successful_return_pub.publish.call_args.args[0]
                event = json.loads(wire.data)
                self.assertEqual(uid, event['uav_id'])
                self.assertEqual('external_program_b_land', event['source'])
                node._land.assert_not_called()
                node._fly_to.assert_not_called()
            finally:
                case.doCleanups()

            images = image_tests.FinalReconciliationTest()
            images.setUp()
            try:
                sender = images.sender
                sender.uav_id = uid
                sender.active_mission_id = event['mission_id']
                sender._successful_return_callback(wire)
                self.assertEqual('successful_return', sender.final_reconcile_trigger)
                sender.final_reconcile_done = True
                sender._schedule_reconciliation.reset_mock()
                sender._successful_return_callback(wire)
                sender._competition_time_callback(types.SimpleNamespace(data=json.dumps(dict(
                    running=True,mission_id=event['mission_id'],session_id='session',elapsed_seconds=1410))))
                sender._reconcile_timer_callback(None)
                sender._schedule_reconciliation.assert_not_called()
            finally:
                images.doCleanups()

            self.assertEqual([uid], plan.observe({str(uid): {'successful_return': event}},
                                                'test', 'session', checksums, 100))
            self.assertFalse(plan.due(100, 1000))
            if uid < 6:
                self.assertFalse(plan.due(102.99, 1000))

        for index in range(10):
            self.assertEqual(['six_returns'], plan.due(103 + 2 * index, 1000))
        self.assertFalse(plan.due(500, 1440))
        self.assertEqual('six_returns', plan.selected_strategy)


if __name__ == '__main__':
    unittest.main()
