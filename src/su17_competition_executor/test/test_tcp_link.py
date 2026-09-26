import json
import socket
import threading
import time
import unittest

from su17_competition_executor.tcp_link import OnboardTcpLink, PROTOCOL_VERSION


def receive_message(connection, buffer):
    while b"\n" not in buffer:
        chunk = connection.recv(65536)
        if not chunk:
            raise OSError("test connection closed")
        buffer.extend(chunk)
    raw, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    return json.loads(raw.decode("utf-8"))


class OnboardTcpLinkTest(unittest.TestCase):
    def test_connects_sends_telemetry_and_receives_command(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(3.0)
        port = server.getsockname()[1]
        commands = []
        command_event = threading.Event()

        def handler(message):
            commands.append(message)
            command_event.set()

        link = OnboardTcpLink(
            uav_id=1,
            ground_host="127.0.0.1",
            ground_port=port,
            auth_token="secret",
            command_handler=handler,
            reconnect_seconds=0.2,
        )
        try:
            link.update_telemetry(
                {
                    "type": "telemetry",
                    "uav_id": 1,
                    "connected": True,
                    "position": [0, 0, 0],
                    "velocity": [0, 0, 0],
                }
            )
            link.start()
            connection, _ = server.accept()
            connection.settimeout(3.0)
            buffer = bytearray()
            hello = receive_message(connection, buffer)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["auth_token"], "secret")
            connection.sendall(
                (
                    json.dumps(
                        {
                            "type": "hello_ack",
                            "uav_id": 1,
                            "protocol_version": PROTOCOL_VERSION,
                        }
                    )
                    + "\n"
                ).encode("utf-8")
            )
            telemetry = receive_message(connection, buffer)
            self.assertEqual(telemetry["type"], "telemetry")

            command = {
                "type": "takeoff",
                "uav_id": 1,
                "mission_id": "m1",
                "target_altitude_m": 0.5,
            }
            connection.sendall((json.dumps(command) + "\n").encode("utf-8"))
            self.assertTrue(command_event.wait(2.0))
            self.assertEqual(commands, [command])
            connection.close()
        finally:
            link.stop()
            server.close()


if __name__ == "__main__":
    unittest.main()
