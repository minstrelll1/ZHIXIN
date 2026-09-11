#!/usr/bin/env python3

import importlib.util
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

receiver_spec = importlib.util.spec_from_file_location(
    "ground_image_receiver", PACKAGE_ROOT / "ground" / "ground_image_receiver.py"
)
receiver_module = importlib.util.module_from_spec(receiver_spec)
receiver_spec.loader.exec_module(receiver_module)

from su17_image_transfer.protocol import encode_frame, receive_ack, receive_response


class GroundReceiverTest(unittest.TestCase):
    def test_receives_and_saves_one_image(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            receiver = receiver_module.GroundImageReceiver(
                "127.0.0.1",
                0,
                Path(temporary_directory),
                "test-token",
                status_interval=0,
                client_timeout=0.05,
            )
            server_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            server_thread.start()

            deadline = time.monotonic() + 2.0
            port = 0
            while time.monotonic() < deadline:
                if receiver.server is not None:
                    port = receiver.server.getsockname()[1]
                    if port:
                        break
                time.sleep(0.01)
            self.assertNotEqual(0, port)

            metadata = {
                "request_id": "integration-001",
                "mission_id": "subject1-test",
                "file_name": "integration-001.jpg",
                "uav_id": 1,
                "requested_at_unix_ns": 1_787_000_000_000_000_000,
                "auth_token": "test-token",
            }
            jpeg = b"\xff\xd8integration-test\xff\xd9"
            with socket.create_connection(("127.0.0.1", port), timeout=2.0) as client:
                client.sendall(encode_frame(metadata, jpeg))
                self.assertTrue(receive_ack(client))

                # A socket timeout is only a periodic wake-up. It must not close
                # a sparse snapshot connection that has been idle.
                time.sleep(0.12)
                after_idle = dict(metadata)
                after_idle.update(
                    {
                        "request_id": "after-idle",
                        "file_name": "after-idle.jpg",
                    }
                )
                client.sendall(encode_frame(after_idle, jpeg))
                self.assertTrue(receive_ack(client))

                mission_start = {
                    "message_type": "mission_start",
                    "mission_id": "subject1-test",
                    "mission_started_at_unix_ns": time.time_ns(),
                    "uav_id": 1,
                    "auth_token": "test-token",
                }
                client.sendall(encode_frame(mission_start, b"\x00"))
                success, response = receive_response(client)
                self.assertTrue(success)
                self.assertEqual("subject1-test", response["mission_id"])

                manifest = {
                    "message_type": "manifest",
                    "mission_id": "subject1-test",
                    "uav_id": 1,
                    "auth_token": "test-token",
                    "file_names": ["integration-001.jpg", "missing-002.jpg"],
                }
                client.sendall(encode_frame(manifest, b"\x00"))
                success, response = receive_response(client)
                self.assertTrue(success)
                self.assertEqual(["missing-002.jpg"], response["missing"])

                recovered_metadata = dict(metadata)
                recovered_metadata.update(
                    {
                        "request_id": "missing-002",
                        "file_name": "missing-002.jpg",
                        "retransmission": True,
                    }
                )
                client.sendall(encode_frame(recovered_metadata, jpeg))
                self.assertTrue(receive_ack(client))

                client.sendall(encode_frame(manifest, b"\x00"))
                success, response = receive_response(client)
                self.assertTrue(success)
                self.assertEqual([], response["missing"])

                uav2_metadata = dict(metadata)
                uav2_metadata.update(
                    {
                        "request_id": "uav2-001",
                        "file_name": "uav2-001.jpg",
                        "uav_id": 2,
                    }
                )
                client.sendall(encode_frame(uav2_metadata, jpeg))
                self.assertTrue(receive_ack(client))

                invalid_metadata = dict(metadata)
                invalid_metadata.update(
                    {
                        "request_id": "uav7-invalid",
                        "file_name": "uav7-invalid.jpg",
                        "uav_id": 7,
                    }
                )
                client.sendall(encode_frame(invalid_metadata, jpeg))
                self.assertFalse(receive_ack(client))

            image_paths = list(Path(temporary_directory).rglob("integration-001.jpg"))
            metadata_paths = list(Path(temporary_directory).rglob("integration-001.json"))
            self.assertEqual(1, len(image_paths))
            self.assertEqual(jpeg, image_paths[0].read_bytes())
            self.assertEqual(1, len(metadata_paths))
            self.assertNotIn("test-token", metadata_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(
                1, len(list((Path(temporary_directory) / "UAV2").rglob("uav2-001.jpg")))
            )
            self.assertFalse((Path(temporary_directory) / "UAV7").exists())

            receiver.stop()
            server_thread.join(timeout=2.0)
            self.assertFalse(server_thread.is_alive())

    def test_six_uavs_can_upload_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            receiver = receiver_module.GroundImageReceiver(
                "127.0.0.1",
                0,
                Path(temporary_directory),
                "",
                allowed_uav_ids=range(1, 7),
                status_interval=0,
            )
            server_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            server_thread.start()

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if receiver.server is not None:
                    port = receiver.server.getsockname()[1]
                    if port:
                        break
                time.sleep(0.01)
            else:
                self.fail("receiver did not start")

            barrier = threading.Barrier(6)
            errors = []

            def upload(uav_id):
                try:
                    metadata = {
                        "message_type": "image",
                        "request_id": "parallel-%d" % uav_id,
                        "mission_id": "six-uav-test",
                        "file_name": "uav%d.jpg" % uav_id,
                        "uav_id": uav_id,
                    }
                    with socket.create_connection(
                        ("127.0.0.1", port), timeout=2.0
                    ) as client:
                        barrier.wait(timeout=2.0)
                        client.sendall(encode_frame(metadata, b"jpeg-%d" % uav_id))
                        if not receive_ack(client):
                            raise AssertionError("receiver rejected UAV%d" % uav_id)
                except Exception as exc:
                    errors.append(exc)

            workers = [
                threading.Thread(target=upload, args=(uav_id,))
                for uav_id in range(1, 7)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=3.0)

            self.assertEqual([], errors)
            for uav_id in range(1, 7):
                self.assertTrue(
                    (
                        Path(temporary_directory)
                        / ("UAV%d" % uav_id)
                        / "six-uav-test"
                        / ("uav%d.jpg" % uav_id)
                    ).exists()
                )

            receiver.stop()
            server_thread.join(timeout=2.0)
            self.assertFalse(server_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
