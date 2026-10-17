import unittest
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
from su17_competition_executor.flight_diagnostics import FlightDiagnostics


class FlightDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.logger = Mock()
        self.record = FlightDiagnostics(clock=lambda: self.now, logger=self.logger)

    def test_recent_warning_keeps_source_and_rejects_old_or_info(self):
        self.record.record('PX4', 2, 'Arming denied: Safety switch')
        self.record.record('PX4', 6, 'Connected')
        self.assertEqual(self.record.recent_warnings(), 'PX4: Arming denied: Safety switch')
        self.now = 116
        self.assertEqual(self.record.recent_warnings(), '')

    def test_repeated_messages_are_bounded_and_log_throttled(self):
        for _ in range(120):
            self.record.record('PX4', 4, 'warning')
        self.assertEqual(self.logger.call_count, 1)
        self.assertEqual(len(self.record.events), 40)
        self.assertEqual(self.record.recent_warnings(), 'PX4: warning')
        self.now += 5
        self.record.record('PX4', 4, 'warning')
        self.assertEqual(self.logger.call_count, 2)

    def test_newest_three_unique_warnings_only(self):
        for i in range(5): self.record.record('PX4', 3, str(i))
        self.assertEqual(self.record.recent_warnings(), 'PX4: 4；PX4: 3；PX4: 2')

    def test_localization_keeps_raw_reason_and_age_without_changing_state(self):
        state=SimpleNamespace(connected=True, armed=False, odom_valid=False, gps_status=1, gps_num=8, location_source=4, mode='ALTCTL')
        self.record.record('Prometheus',3,'Odom invalid: GPS/RTK location error!',source_stamp=90.)
        self.record._sample('gps_raw',SimpleNamespace(fix_type=1,eph=float('nan')),{'fix_type':'fix_type','eph':'eph'})
        self.now+=2
        data=self.record.localization_snapshot(state,None,0.1,None,None,'gps_fix_below_3d')
        self.assertFalse(state.odom_valid)
        self.assertEqual(data['samples']['gps_raw']['age_seconds'],2)
        self.assertIsNone(data['samples']['gps_raw']['eph'])
        self.assertEqual(data['alerts'][0]['source_stamp'],90.)
        self.assertEqual(data['state']['gps_num'],8)
        json.dumps(data,allow_nan=False)

    def test_no_state_still_has_diagnostic_and_static_snapshot_is_throttled(self):
        first=self.record.localization_snapshot(None,None,None,None,None,'flight_state_missing_or_disconnected')
        for _ in range(20): self.record.localization_snapshot(None,None,None,None,None,'flight_state_missing_or_disconnected')
        self.assertEqual(self.logger.call_count,1)
        self.assertIsNone(first['state']['odom_valid'])
        self.now+=5
        self.record.localization_snapshot(None,None,None,None,None,'flight_state_missing_or_disconnected')
        self.assertEqual(self.logger.call_count,2)

    def test_onboard_file_keeps_alert_without_ground_link_and_failure_is_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            self.record.configure_storage(directory,'boot-A',3)
            self.record.record('PX4',3,'GPS not ready',source_stamp=99.)
            self.record.localization_snapshot(None,None,None,None,None,'state_missing')
            rows=[json.loads(line) for line in (Path(directory)/'diagnostics.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([r['event'] for r in rows],['diagnostic_started','flight_alert','localization_snapshot'])
            self.assertTrue(all(r['boot_id']=='boot-A' for r in rows))
            self.record.close()
        self.record.storage=Mock()
        self.record.storage.handle.side_effect=OSError('disk full')
        self.record.record('PX4',3,'another warning')
        self.assertIn('another warning',self.record.recent_warnings())

    def test_wire_is_throttled_but_invalidity_or_new_alert_is_immediate(self):
        state=SimpleNamespace(connected=True,odom_valid=True)
        def snapshot(): return self.record.localization_snapshot(state,None,0.,None,None,'recorded')
        first=snapshot()
        self.assertIs(self.record.wire_snapshot(first),first)
        for _ in range(24):
            self.now+=.2
            self.assertIsNone(self.record.wire_snapshot(snapshot()))
        state.odom_valid=False
        self.assertIsNotNone(self.record.wire_snapshot(snapshot()))
        self.assertIsNone(self.record.wire_snapshot(snapshot()))
        self.record.record('PX4',3,'position failure')
        self.assertIsNotNone(self.record.wire_snapshot(snapshot()))
        self.now+=5
        self.assertIsNotNone(self.record.wire_snapshot(snapshot()))


if __name__ == '__main__':
    unittest.main()
