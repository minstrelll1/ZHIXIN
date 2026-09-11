import json
import tempfile
import unittest
from pathlib import Path

from su17_competition_executor.task_protocol import (
    TaskValidationError,
    assignment_checksum,
    load_assignment,
    save_assignment_atomic,
    validate_assignment,
)


def assignment():
    value = {
        "type": "assign_task",
        "uav_id": 1,
        "mission_id": "subject1-test",
        "subject": "subject1",
        "target_altitude_m": 1.0,
        "task": {
            "type": "lawnmower_search",
            "coordinate_frame": "ENU",
            "sector": 1,
            "bounds_m": {"x_min": 0, "x_max": 1, "y_min": 0, "y_max": 1.5},
            "lane_spacing_m": 0.5,
            "waypoints_m": [[0, 0], [1, 0], [1, 0.5], [0, 0.5]],
        },
    }
    value["assignment_checksum"] = assignment_checksum(value)
    return value


class TaskProtocolTest(unittest.TestCase):
    def test_validate_and_persist(self):
        normalized = validate_assignment(assignment(), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "assigned.json")
            save_assignment_atomic(path, normalized)
            self.assertEqual(load_assignment(path), normalized)

    def test_rejects_wrong_uav(self):
        with self.assertRaises(TaskValidationError):
            validate_assignment(assignment(), 2)

    def test_rejects_nonfinite_waypoint(self):
        value = assignment()
        value["task"]["waypoints_m"][0][0] = float("nan")
        with self.assertRaises(TaskValidationError):
            validate_assignment(value, 1)

    def test_rejects_changed_task_after_checksum(self):
        value = assignment()
        value["task"]["waypoints_m"][1][0] = 99
        with self.assertRaises(TaskValidationError):
            validate_assignment(value, 1)


if __name__ == "__main__":
    unittest.main()
