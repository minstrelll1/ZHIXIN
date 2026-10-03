import unittest
from competition_backend.return_report_schedule import ReturnReportSchedule


def telemetry(count=6, age=0):
    return {str(i): {'successful_return': dict(phase='return_descent', uav_id=i,
             mission_id='subject1-a', assignment_checksum='checksum-'+str(i),
             competition_session_id='session', event_id='return-'+str(i), age_seconds=age)}
            for i in range(1, count+1)}


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.plan = ReturnReportSchedule()
        self.checksums = {str(i): 'checksum-'+str(i) for i in range(1, 7)}

    def observe(self, data, now):
        return self.plan.observe(data, 'subject1-a', 'session', self.checksums, now)

    def test_five_no_trigger_sixth_wait_three_seconds_ten_at_two_seconds(self):
        self.observe(telemetry(5), 10)
        self.assertFalse(self.plan.due(100, 1000))
        self.observe(telemetry(), 100)
        self.assertFalse(self.plan.due(102.999, 1000))
        for i in range(10):
            self.assertEqual(['six_returns'], self.plan.due(103+2*i, 1000))
            self.assertFalse(self.plan.due(104+2*i, 1000))
        self.assertFalse(self.observe(telemetry(), 200))
        self.assertFalse(self.plan.due(201, 1000))

    def test_return_wins_before_timer_even_when_delay_crosses_24_minutes(self):
        self.observe(telemetry(), 97)
        self.assertFalse(self.plan.due(97, 1439))
        self.assertEqual('six_returns',self.plan.selected_strategy)
        self.assertFalse(self.plan.due(98, 1440))
        for n in range(10):
            self.assertEqual(['six_returns'],self.plan.due(100+2*n,1442+2*n))
        self.assertFalse(self.plan.due(140,1482))
        self.assertEqual(0,self.plan.snapshot()['clock_attempts'])

    def test_clock_wins_and_late_sixth_return_does_not_add_requests(self):
        self.observe(telemetry(5),97)
        self.assertEqual(['competition_time'],self.plan.due(100,1440))
        self.observe(telemetry(),101)
        self.assertFalse(self.plan.due(104,1444))
        self.assertEqual(['competition_time'],self.plan.due(105,1445))
        self.assertEqual(0,self.plan.return_attempts)
        self.assertFalse(self.plan.due(190,1530))

    def test_delayed_telemetry_compares_sixth_event_age_to_timer(self):
        self.observe(telemetry(age=20),100)
        self.assertEqual(['six_returns'],self.plan.due(100,1445))
        self.assertEqual('six_returns',self.plan.selected_strategy)

    def test_clock_window_eighteen_slots_no_rewind_replay_or_catchup(self):
        self.assertFalse(self.plan.due(0, 1439.99))
        for n in range(18):
            self.assertEqual(['competition_time'], self.plan.due(100+5*n, 1440+5*n))
        self.assertFalse(self.plan.due(190, 1530))
        self.assertFalse(self.plan.due(195, 1440))
        later = ReturnReportSchedule()
        self.assertEqual(['competition_time'], later.due(0, 1452))
        self.assertFalse(later.due(1, 1453))
        self.assertEqual(1, later.snapshot()['clock_attempts'])

    def test_reconnect_uses_event_age_without_counting_old_task_or_generic_landing(self):
        data = telemetry()
        data['1']['successful_return']['phase'] = 'landing'
        data['2']['successful_return']['mission_id'] = 'subject1-old'
        data['3']['successful_return']['competition_session_id'] = 'old'
        data['4']['successful_return']['assignment_checksum'] = 'old'
        data['5']['successful_return']['uav_id'] = 6
        self.assertEqual([6], self.observe(data, 0))
        self.assertFalse(self.plan.due(5, 100))
        self.observe(telemetry(age=20), 100)
        self.assertEqual(['six_returns'], self.plan.due(100, 100))

    def test_report_provider_can_be_dataclass_and_nonfinite_age_rejected(self):
        from competition_backend.models import Telemetry
        data = telemetry(1)
        data['1']['successful_return']['age_seconds'] = float('nan')
        self.assertFalse(self.observe(data, 0))
        event = telemetry(1)['1']['successful_return']
        self.assertEqual([1], self.observe({1: Telemetry(1, 0, successful_return=event)}, 0))


if __name__ == '__main__':
    unittest.main()
