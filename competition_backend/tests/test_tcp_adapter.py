import json
import socket
import threading
import time
import unittest

from competition_backend.tcp_adapter import PROTOCOL_VERSION, TcpFleetAdapter


def send_message(connection, payload):
    connection.sendall(
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    )


def receive_message(connection, buffer):
    deadline = time.time() + 3.0
    while b"\n" not in buffer:
        if time.time() >= deadline:
            raise TimeoutError("TCP test message timed out")
        chunk = connection.recv(65536)
        if not chunk:
            raise OSError("TCP test connection closed")
        buffer.extend(chunk)
    raw, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    return json.loads(raw.decode("utf-8"))


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TcpFleetAdapterTest(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.lock = threading.Lock()
        self.adapter = TcpFleetAdapter(
            [1, 2], bind_host="127.0.0.1", port=0, auth_token="test-token"
        )

        def sink(telemetry):
            with self.lock:
                self.received.append(telemetry)

        self.adapter.set_telemetry_sink(sink)
        self.adapter.start()
        self.clients = []

    def tearDown(self):
        for connection, _ in self.clients:
            try:
                connection.close()
            except OSError:
                pass
        self.adapter.stop()

    def connect_uav(self, uav_id):
        connection = socket.create_connection(
            ("127.0.0.1", self.adapter.bound_port), timeout=2.0
        )
        connection.settimeout(3.0)
        buffer = bytearray()
        send_message(
            connection,
            {
                "type": "hello",
                "protocol_version": PROTOCOL_VERSION,
                "uav_id": uav_id,
                "auth_token": "test-token",
            },
        )
        hello = receive_message(connection, buffer)
        self.assertEqual(hello["type"], "hello_ack")
        self.clients.append((connection, buffer))
        return connection, buffer

    def test_two_uavs_exchange_independent_commands_and_telemetry(self):
        client1, buffer1 = self.connect_uav(1)
        client2, buffer2 = self.connect_uav(2)
        for uav_id, client in ((1, client1), (2, client2)):
            send_message(
                client,
                {
                    "type": "telemetry",
                    "uav_id": uav_id,
                    "connected": True,
                    "armed": True,
                    "odom_valid": True,
                    "failsafe": False,
                    "control_state": 2,
                    "battery_percentage": 0.8 + 0.01 * uav_id,
                    "position": [uav_id, 0, 0.5],
                    "velocity": [0, 0, 0],
                },
            )
        self.assertTrue(
            wait_until(
                lambda: {item.uav_id for item in self.received if item.connected}
                == {1, 2}
            )
        )

        self.adapter.command_takeoff(1, {"mission_id": "m1", "target_altitude_m": 0.5})
        self.adapter.command_takeoff(2, {"mission_id": "m1", "target_altitude_m": 1.5})
        command1 = receive_message(client1, buffer1)
        command2 = receive_message(client2, buffer2)
        self.assertEqual((command1["uav_id"], command1["target_altitude_m"]), (1, 0.5))
        self.assertEqual((command2["uav_id"], command2["target_altitude_m"]), (2, 1.5))

        client1.shutdown(socket.SHUT_RDWR)
        client1.close()
        self.assertTrue(
            wait_until(
                lambda: any(
                    item.uav_id == 1 and not item.connected
                    for item in self.received
                )
            )
        )
        self.adapter.command_task(2, {"mission_id": "m1", "task": {"sector": 5}})
        self.assertEqual(receive_message(client2, buffer2)["type"], "execute_task")

    def test_assignment_ack_requires_manual_dispatch_after_connection(self):
        with self.assertRaises(RuntimeError):
            self.adapter.command_assign_task(1, {"mission_id": "offline"})
        client, buffer = self.connect_uav(1)
        client.settimeout(0.15)
        with self.assertRaises(socket.timeout):
            client.recv(1)
        client.settimeout(3.0)
        self.adapter.command_assign_task(
            1,
            {
                "mission_id": "mission-new",
                "assignment_checksum": "abc123",
                "task": {"type": "lawnmower_search"},
                "target_altitude_m": 0.5,
            },
        )
        assignment = receive_message(client, buffer)
        self.assertEqual(assignment["type"], "assign_task")
        self.assertEqual(assignment["mission_id"], "mission-new")

        send_message(
            client,
            {
                "type": "task_status",
                "uav_id": 1,
                "status": {
                    "state": "task_received",
                    "task_assignment_acked": True,
                    "mission_id": "mission-new",
                    "assignment_checksum": "abc123",
                },
            },
        )
        self.assertTrue(
            wait_until(
                lambda: any(
                    item.uav_id == 1
                    and item.task_assignment_mission_id == "mission-new"
                    and item.task_assignment_checksum == "abc123"
                    for item in self.received
                )
            )
        )

    def test_assignment_send_does_not_wait_for_telemetry_callback(self):
        client, buffer = self.connect_uav(2)
        callback_entered = threading.Event()
        release_callback = threading.Event()
        send_finished = threading.Event()
        errors = []

        def sink(telemetry):
            # 模拟遥测回调正在等待规划线程持有的任务状态锁。
            callback_entered.set()
            release_callback.wait(5.0)

        def assign():
            try:
                self.adapter.command_assign_task(2, {
                    "mission_id": "concurrent-plan",
                    "assignment_checksum": "new-task",
                })
            except Exception as error:
                errors.append(error)
            finally:
                send_finished.set()

        self.adapter.set_telemetry_sink(sink)
        sender = threading.Thread(target=assign, daemon=True)
        try:
            send_message(client, {"type": "heartbeat", "uav_id": 2})
            self.assertTrue(callback_entered.wait(2.0))
            sender.start()
            self.assertTrue(
                send_finished.wait(1.0),
                "遥测回调持有 TCP 锁，规划下发会与任务状态锁形成死锁",
            )
            self.assertEqual(errors, [])
            assignment = receive_message(client, buffer)
            self.assertEqual(assignment["type"], "assign_task")
            self.assertEqual(assignment["mission_id"], "concurrent-plan")
        finally:
            release_callback.set()
            if sender.ident is not None:
                sender.join(3.0)

    def test_replaced_connection_cannot_update_telemetry(self):
        self.connect_uav(1)
        with self.adapter._lock:
            previous = self.adapter._clients[1]
        self.connect_uav(1)
        received = []
        self.adapter.set_telemetry_sink(received.append)

        handled = self.adapter._handle_message(1, {
            "type": "task_status",
            "status": {
                "state": "task_received",
                "mission_id": "obsolete-task",
                "task_assignment_acked": True,
            },
        }, expected_client=previous)

        self.assertFalse(handled)
        self.assertEqual(received, [])
        with self.adapter._lock:
            self.assertFalse(self.adapter._telemetry[1].task_assignment_acked)


if __name__ == "__main__":
    unittest.main()
