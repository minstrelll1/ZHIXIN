import struct
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from su17_competition_executor.ego_state_bridge import EgoStateReadOnlyBridge


class _IntegerMessage:
    def deserialize(self, payload):
        self.data = struct.unpack("<i", payload)[0]


class EgoStateBridgeTest(unittest.TestCase):
    def test_ros_state_zero_and_age_are_preserved(self):
        ros = types.SimpleNamespace(AnyMsg=object, Subscriber=Mock(), logwarn=Mock())
        bridge = EgoStateReadOnlyBridge(ros, 4)
        self.assertEqual(bridge.topic, "/uav4/ego_planner/exec_state")
        self.assertEqual(bridge.snapshot(), (None, None))

        raw = types.SimpleNamespace(
            _connection_header={"type": "std_msgs/Int32"},
            _buff=struct.pack("<i", 0),
        )
        ros_messages = types.SimpleNamespace(get_message_class=lambda _: _IntegerMessage)
        with patch("su17_competition_executor.ego_state_bridge.importlib.import_module",
                   return_value=ros_messages):
            bridge._receive(raw)
            state, age = bridge.snapshot()
            self.assertEqual(state, 0)
            self.assertGreaterEqual(age, 0.0)
            bridge._receive(types.SimpleNamespace(
                _connection_header={"type": "std_msgs/Int32"},
                _buff=struct.pack("<i", 7),
            ))
            self.assertEqual(bridge.snapshot()[0], 0)
            bridge._receive(types.SimpleNamespace(
                _connection_header={"type": "std_msgs/Int32"},
                _buff=struct.pack("<i", 5),
            ))
            self.assertEqual(bridge.snapshot()[0], 5)


if __name__ == "__main__":
    unittest.main()
