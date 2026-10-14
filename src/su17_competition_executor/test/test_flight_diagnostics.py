import unittest
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


if __name__ == '__main__':
    unittest.main()
