#!/usr/bin/env python3

import socket
from pathlib import Path
import sys
import threading
import unittest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from su17_image_transfer.protocol import (
    ProtocolError,
    encode_frame,
    receive_ack,
    receive_frame,
    receive_response,
    send_ack,
    send_response,
)


class ProtocolTest(unittest.TestCase):
    def test_frame_and_ack_round_trip(self):
        left, right = socket.socketpair()
        metadata = {"request_id": "capture-1", "uav_id": 1}
        jpeg = b"\xff\xd8test-jpeg\xff\xd9"

        def receiver():
            actual_metadata, actual_jpeg = receive_frame(right)
            self.assertEqual(metadata, actual_metadata)
            self.assertEqual(jpeg, actual_jpeg)
            send_ack(right, True)

        thread = threading.Thread(target=receiver)
        thread.start()
        left.sendall(encode_frame(metadata, jpeg))
        self.assertTrue(receive_ack(left))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        left.close()
        right.close()

    def test_empty_jpeg_is_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_frame({"request_id": "bad"}, b"")

    def test_structured_response_round_trip(self):
        left, right = socket.socketpair()
        expected = {"missing": ["image-002.jpg"], "present": 1}
        send_response(right, True, expected)
        success, actual = receive_response(left)
        self.assertTrue(success)
        self.assertEqual(expected, actual)
        left.close()
        right.close()


if __name__ == "__main__":
    unittest.main()
