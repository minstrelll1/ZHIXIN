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
        self.plan.set_participants(range(1, 7))
        self.checksums = {str(i): 'checksum-'+str(i) for i in range(1, 7)}

    def observe(self, data, now):
        return self.plan.observe(data, 'subject1-a', 'session', self.checksums, now)

    def test_five_no_trigger_sixth_wait_three_seconds_ten_at_two_seconds(self):
        self.observe(telemetry(5), 10)
        self.assertFalse(self.plan.due(100, 1000))
        self.observe(telemetry(), 100)
        self.assertFalse(self.plan.due(102.999, 1000))
        for i in range(10):
            self.assertEqual(['all_participants_returned'], self.plan.due(103+2*i, 1000))
            self.assertFalse(self.plan.due(104+2*i, 1000))
        self.assertFalse(self.observe(telemetry(), 200))
        self.assertFalse(self.plan.due(201, 1000))

    def test_return_wins_before_timer_even_when_delay_crosses_24_minutes(self):
        self.observe(telemetry(), 97)
        self.assertFalse(self.plan.due(97, 1439))
        self.assertEqual('all_participants_returned',self.plan.selected_strategy)
        self.assertFalse(self.plan.due(98, 1440))
        for n in range(10):
            self.assertEqual(['all_participants_returned'],self.plan.due(100+2*n,1442+2*n))
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
        self.assertEqual(['all_participants_returned'],self.plan.due(100,1445))
        self.assertEqual('all_participants_returned',self.plan.selected_strategy)

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
        self.assertEqual(['all_participants_returned'], self.plan.due(100, 100))

    def test_report_provider_can_be_dataclass_and_nonfinite_age_rejected(self):
        from competition_backend.models import Telemetry
        data = telemetry(1)
        data['1']['successful_return']['age_seconds'] = float('nan')
        self.assertFalse(self.observe(data, 0))
        event = telemetry(1)['1']['successful_return']
        self.assertEqual([1], self.observe({1: Telemetry(1, 0, successful_return=event)}, 0))


class PartialFleetTest(unittest.TestCase):
    def setUp(self):
        self.plan = ReturnReportSchedule()
        self.checksums = {str(i): 'checksum-'+str(i) for i in range(1, 7)}

    def observe(self, ids, now):
        data = {uid: value for uid, value in telemetry().items() if int(uid) in ids}
        return self.plan.observe(data, 'subject1-a', 'session', self.checksums, now)

    def test_actual_four_flight_last_return_then_three_seconds(self):
        self.plan.set_participants([1, 2, 4, 5])
        # 复现15:34:00、07、10、37依次收到5、4、2、1的成功返航。
        for uid, now in [(5, 0), (4, 7), (2, 10)]:
            self.observe([uid], now)
            self.assertFalse(self.plan.due(now, 820+now))
        self.assertEqual([1], self.plan.snapshot()['pending_return_uav_ids'])
        # 3、6即使出现本任务标记，也不是本次起飞机，不参与计数。
        self.assertFalse(self.observe([3, 6], 36))
        self.observe([1], 37)
        self.assertFalse(self.plan.due(39.99, 860))
        for n in range(10):
            self.assertEqual(['all_participants_returned'], self.plan.due(40+2*n, 860+2*n))
        self.assertFalse(self.plan.due(1000, 1440))
        self.assertEqual([1, 2, 4, 5], self.plan.snapshot()['participant_uav_ids'])

    def test_no_takeoff_no_return_trigger_and_no_shrink_on_disconnect(self):
        self.assertFalse(self.observe(range(1, 7), 0))
        self.assertFalse(self.plan.due(4, 1000))
        self.plan.set_participants([1, 2])
        self.observe([1], 10)
        with self.assertRaises(ValueError):
            self.plan.set_participants([1])
        self.assertFalse(self.plan.due(100, 1000))
        self.assertEqual(['competition_time'], self.plan.due(100, 1440))

    def test_single_aircraft_and_empty_list_not_vacuously_complete(self):
        with self.assertRaises(ValueError):
            self.plan.set_participants([])
        self.plan.set_participants([5])
        self.observe([5], 10)
        self.assertFalse(self.plan.due(12.99, 900))
        self.assertEqual(['all_participants_returned'], self.plan.due(13, 900))


if __name__ == '__main__':
    unittest.main()
