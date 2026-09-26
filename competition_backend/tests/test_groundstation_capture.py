import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.groundstation_capture import (
    GroundStationStream, PassivePointCloudCapture, TcpStream, WebSocketMessages, pcapng_ipv4,
)
from competition_backend.pcl_octree import decode_xyz
from competition_backend.pointcloud import decode_pointcloud2

FIXTURE = Path(__file__).parent / "fixtures" / "pcl_xyz_three_points.bin"
TOPIC = "/uav1/octomap_point_cloud_centers/reduce_the_frequency/compressed"


def ws(payload, first=0x81):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    if len(data) < 126:
        header = bytes([first, len(data)])
    elif len(data) < 65536:
        header = bytes([first, 126]) + struct.pack("!H", len(data))
    else:
        header = bytes([first, 127]) + struct.pack("!Q", len(data))
    return header + data


def packet(sequence, data, target_port=7557, source="192.168.1.88", flags=0x18):
    ip = bytearray(20)
    ip[0], ip[9] = 0x45, 6
    struct.pack_into("!H", ip, 2, 40 + len(data))
    ip[12:16], ip[16:20] = socket.inet_aton(source), socket.inet_aton("192.168.1.123")
    tcp = bytearray(20)
    struct.pack_into("!HHI", tcp, 0, 9090, target_port, sequence)
    tcp[12], tcp[13] = 0x50, flags
    return bytes(ip + tcp) + data


def publish():
    raw = FIXTURE.read_bytes()
    return {"op": "publish", "topic": TOPIC, "_competition_capture": "pcap_replay", "msg": {
        "header": {"frame_id": "world", "stamp": {"secs": 123, "nsecs": 456}},
        "width": 0, "height": 0, "fields": [], "point_step": 0, "row_step": len(raw),
        "data": base64.b64encode(raw).decode("ascii")}}


class PclTest(unittest.TestCase):
    def test_pcl_reference_xyz(self):
        body, metadata = decode_xyz(FIXTURE.read_bytes())
        self.assertEqual(metadata["source_points"], 3)
        # 来自 PCL 1.10.0 C++ 静态熵解码及 float32 点坐标重建。
        self.assertEqual(hashlib.sha256(body).hexdigest(),
                         "322c8118e95729a2e89b360e4f548580460fc4d0b9ecf9a6c3d2c159cf58d6dd")

    def test_invalid_frames_rejected(self):
        raw = FIXTURE.read_bytes()
        corrupt_frequency = bytearray(raw)
        struct.pack_into("<I", corrupt_frequency, 108, 123)  # 第一频率必须为 0
        enormous = bytearray(raw)
        struct.pack_into("<Q", enormous, 27, 2 ** 63)
        pframe = bytearray(raw)
        pframe[24] = 0
        for invalid in (raw[:10], raw[:-1], raw + b"extra", b"wrong" + raw[5:],
                        bytes(corrupt_frequency), bytes(enormous), bytes(pframe)):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(ValueError):
                    decode_xyz(invalid)

    def test_pointcloud_envelope_and_row_padding(self):
        points, metadata = decode_pointcloud2(publish()["msg"], max_points=2)
        self.assertEqual(len(points), 2)
        self.assertEqual(metadata["source_points"], 3)
        self.assertEqual(metadata["frame_id"], "world")
        msg = {"width": 1, "height": 2, "point_step": 12, "row_step": 16,
               "fields": [{"name": n, "offset": i * 4, "datatype": 7} for i, n in enumerate("xyz")],
               "data": base64.b64encode(struct.pack("<fff4xfff4x", 1, 2, 3, 4, 5, 6)).decode()}
        self.assertEqual(decode_pointcloud2(msg, 20)[0], [[1, 2, 3], [4, 5, 6]])
        msg["data"] = base64.b64encode(b"truncated").decode()
        with self.assertRaises(ValueError):
            decode_pointcloud2(msg, 20)


class StreamTest(unittest.TestCase):
    def test_websocket_chunking_sizes_and_midstream(self):
        for size in (10, 200, 70000):
            expected = {"text": "a" * size}
            data = b"partial prior JSON frame" + ws(expected)
            parser = WebSocketMessages()
            result = []
            for i in range(0, len(data), 31):
                result.extend(parser.feed(data[i:i+31]))
            self.assertEqual(result, [expected])

    def test_websocket_fragmented_json_with_ping(self):
        parser = WebSocketMessages()
        self.assertEqual(parser.feed(ws(b'{"a":', 0x01) + ws(b'hi', 0x89)), [])
        self.assertEqual(parser.feed(ws(b'42}', 0x80)), [{"a": 42}])

    def test_tcp_reorder_overlap_and_sequence_wrap(self):
        for initial in (100, 2**32-10):
            parser = TcpStream()
            data = ws({"op": "publish", "value": 42})
            result = parser.feed(initial, data[:10], 0)
            result += parser.feed((initial+20) % 2**32, data[20:], .1)
            result += parser.feed(initial, data[:10], .2)
            result += parser.feed((initial+5) % 2**32, data[5:20], .3)
            self.assertEqual(result, [{"op": "publish", "value": 42}])

    def test_missing_segment_does_not_create_a_frame(self):
        parser = TcpStream()
        data = ws({"op": "publish", "value": "old incomplete"})
        self.assertEqual(parser.feed(100, data[:10], 0), [])
        self.assertEqual(parser.feed(120, data[20:], .1), [])
        new = {"value": "new intact"}
        self.assertEqual(parser.feed(100 + len(data), ws(new), 3), [new])
        self.assertEqual(parser.resets, 1)

    def test_independent_connections_and_remote_filter(self):
        parser = GroundStationStream("192.168.1.123", "192.168.1.88")
        a, b = ws({"a": 1}), ws({"b": 2})
        self.assertEqual(parser.feed(packet(10, a[:5])), [])
        self.assertEqual(parser.feed(packet(10, b, target_port=7558)), [{"b": 2}])
        self.assertEqual(parser.feed(packet(10, b, source="192.168.1.99")), [])
        self.assertEqual(parser.feed(packet(15, a[5:])), [{"a": 1}])

    def test_pcapng_endianness_and_truncation(self):
        for endian in ("<", ">"):
            def block(kind, body):
                size = len(body) + 12
                return struct.pack(endian+"II", kind, size) + body + struct.pack(endian+"I", size)
            ip = packet(10, ws({"a": 1}))
            ethernet = b"\x00" * 12 + b"\x08\x00" + ip
            data = block(0x0a0d0d0a, struct.pack(endian+"IHHq", 0x1a2b3c4d, 1, 0, -1))
            data += block(1, struct.pack(endian+"HHI", 1, 0, 65535))
            data += block(6, struct.pack(endian+"5I", 0, 0, 0, len(ethernet), len(ethernet)) +
                          ethernet + bytes((-len(ethernet)) % 4))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sample.pcapng"
                path.write_bytes(data)
                self.assertEqual(list(pcapng_ipv4(path)), [ip])
                path.write_bytes(data[:-1])
                with self.assertRaises(ValueError):
                    list(pcapng_ipv4(path))

    def test_capture_permission_error_is_visible(self):
        capture = PassivePointCloudCapture("192.168.1.123", "192.168.1.88", 9090, 3, TOPIC, lambda *args: None)
        with patch("competition_backend.groundstation_capture.os.name", "nt"), \
             patch("competition_backend.groundstation_capture.socket.socket", side_effect=PermissionError("10013")):
            capture._capture()
        self.assertFalse(capture.status()["running"])
        self.assertIn("管理员", capture.status()["error"])


class CaptureApiTest(unittest.TestCase):
    def test_uav1_source_maps_to_web_uav3_and_requires_token(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": directory,
                           "COMPETITION_POINTCLOUD_ROOT": directory,
                           "COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP": "",
                           "COMPETITION_POINTCLOUD_SOURCE": "groundstation_shared",
                           "COMPETITION_POINTCLOUD_RELAY_UAV_IDS": "3",
                           "COMPETITION_POINTCLOUD_TOPICS": "3=" + TOPIC,
                           "COMPETITION_POINTCLOUD_INGEST_TOKEN": "test-ingest"}
            with patch.dict(os.environ, environment):
                with TestClient(create_app()) as client:
                    payload = publish()
                    self.assertEqual(client.post("/api/v1/pointcloud/3/ingest", json=payload).status_code, 401)
                    headers = {"X-Pointcloud-Token": "test-ingest"}
                    response = client.post("/api/v1/pointcloud/3/ingest", json=payload, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    frame = client.get("/api/v1/pointcloud/3/latest").json()
                    self.assertEqual((frame["uav_id"], frame["source_points"], frame["topic"]), (3, 3, TOPIC))
                    self.assertEqual(frame["capture_mode"], "pcap_replay")
                    self.assertEqual(client.get("/api/v1/pointcloud/1/latest").status_code, 404)
                    pcd = client.get("/api/v1/pointcloud/3/pcd")
                    self.assertEqual(pcd.status_code, 200)
                    self.assertIn(b"POINTS 3", pcd.content)
                    payload["topic"] = "/uav3/wrong"
                    self.assertEqual(client.post("/api/v1/pointcloud/3/ingest", json=payload, headers=headers).status_code, 422)


if __name__ == "__main__":
    unittest.main()
