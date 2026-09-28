import unittest

import test_orchestrator as existing


class AutoTakeoffPreflightTest(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.OrchestratorTest()
        self.fixture.setUp()
        self.backend = self.fixture.backend
        self.backend.plan('subject1')
        for uid in range(1, 7):
            self.fixture.telemetry(uid, armed=False, control_state=0, capabilities={
                'auto_takeoff_supported': True, 'auto_takeoff_ready': True})

    def test_unarmed_initial_mode_can_prepare_but_only_confirmation_sends_takeoff(self):
        prepared = self.backend.prepare_takeoff()
        self.assertTrue(prepared['ready'])
        self.assertFalse(any(c['type']=='takeoff' for c in self.fixture.adapter.commands))
        self.backend.confirm_takeoff(prepared['confirmation_token'])
        self.assertEqual(len([c for c in self.fixture.adapter.commands if c['type']=='takeoff']), 6)

    def test_older_onboard_still_requires_manual_arming_and_mode(self):
        self.fixture.telemetry(1, armed=False, control_state=0, capabilities={})
        failures=self.backend.preflight_report()['failures']['1']
        self.assertIn('UAV is not armed', failures)
        self.assertIn('control_state is not COMMAND_CONTROL', failures)

    def test_auto_takeoff_does_not_bypass_faults_or_assignment_ack(self):
        for overrides, expected in [
            ({'odom_valid':False},'odometry is invalid'),
            ({'failsafe':True},'failsafe is active'),
            ({'task_assignment_acked':False},'task assignment is not acknowledged'),
            ({'received_at':900},'telemetry is stale'),
        ]:
            with self.subTest(expected=expected):
                self.fixture.telemetry(1, armed=False, control_state=0,
                    capabilities={'auto_takeoff_supported':True,'auto_takeoff_ready':True}, **overrides)
                self.assertIn(expected,self.backend.preflight_report()['failures']['1'])

    def test_startup_condition_changes_between_prepare_and_confirmation(self):
        prepared=self.backend.prepare_takeoff()
        self.fixture.telemetry(1, armed=False, control_state=0, capabilities={
            'auto_takeoff_supported':True,'auto_takeoff_ready':False,'auto_takeoff_reason':'遥控器信号超时'})
        with self.assertRaisesRegex(Exception,'preflight changed'):
            self.backend.confirm_takeoff(prepared['confirmation_token'])
        self.assertFalse(any(c['type']=='takeoff' for c in self.fixture.adapter.commands))

    def test_disarmed_or_rc_mode_cannot_be_marked_at_altitude(self):
        prepared=self.backend.prepare_takeoff()
        self.backend.confirm_takeoff(prepared['confirmation_token'])
        for overrides in ({'armed':False, 'control_state':0},{'armed':True, 'control_state':1}):
            self.fixture.telemetry(1, position=[0,0,1.0], **overrides)
            self.backend.tick()
            self.assertEqual(self.backend.snapshot()['mission']['uavs']['1']['phase'],'takeoff_commanded')


    def test_failed_startup_releases_mission_only_after_every_uav_is_disarmed(self):
        prepared=self.backend.prepare_takeoff()
        self.backend.confirm_takeoff(prepared['confirmation_token'])
        for uid in range(1,7):
            self.fixture.telemetry(uid, armed=(uid==1), task_phase='takeoff_failed')
        self.backend.tick()
        self.assertEqual(self.backend.snapshot()['mission']['uavs']['1']['phase'],'error')
        self.assertNotEqual(self.backend.snapshot()['mission']['phase'],'failed')
        self.fixture.telemetry(1,armed=False,task_phase='takeoff_failed')
        self.backend.tick()
        self.assertEqual(self.backend.snapshot()['mission']['phase'],'failed')

    def test_rc_takeover_never_restarts_task_when_deadline_passes(self):
        prepared=self.backend.prepare_takeoff()
        self.backend.confirm_takeoff(prepared['confirmation_token'])
        self.fixture.clock.advance(101)
        for uid in range(1,7):
            self.fixture.telemetry(uid,armed=True,task_phase='manual_override')
        before=len(self.fixture.adapter.commands)
        self.backend.tick()
        self.assertEqual(len(self.fixture.adapter.commands),before)
        self.assertTrue(all(v['phase']=='error' for v in self.backend.snapshot()['mission']['uavs'].values()))

if __name__=='__main__':
    unittest.main()
