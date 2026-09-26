import base64
import struct
import tempfile
import unittest
from pathlib import Path

from competition_backend.pointcloud import (
    DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
    POINTCLOUD_SOURCE_GROUNDSTATION_RELAY,
    POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
    PointCloudFrame,
    RosbridgePointCloudCollector,
    decode_pointcloud2,
    parse_pointcloud_hosts,
    parse_pointcloud_topics,
    parse_pointcloud_uav_ids,
    pointcloud_topic_from_template,
)


class PointCloudTest(unittest.TestCase):
    def test_decode_pointcloud2_xyz_and_downsample(self):
        raw = b"".join(struct.pack("<fff", *point) for point in ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))
        points, metadata = decode_pointcloud2(
            {
                "header": {"frame_id": "world", "stamp": {"secs": 4, "nsecs": 500000000}},
                "height": 1,
                "width": 2,
                "point_step": 12,
                "fields": [
                    {"name": "x", "offset": 0, "datatype": 7, "count": 1},
                    {"name": "y", "offset": 4, "datatype": 7, "count": 1},
                    {"name": "z", "offset": 8, "datatype": 7, "count": 1},
                ],
                "data": base64.b64encode(raw).decode("ascii"),
            },
            max_points=1,
        )
        self.assertEqual(points, [[1.0, 2.0, 3.0]])
        self.assertEqual(metadata["source_points"], 2)
        self.assertEqual(metadata["frame_id"], "world")

    def test_groundstation_local_relay_configuration(self):
        self.assertEqual(parse_pointcloud_uav_ids("1,3,3,6", range(1, 7)), [1, 3, 6])
        self.assertEqual(parse_pointcloud_uav_ids("", [2, 4]), [2, 4])
        self.assertEqual(
            pointcloud_topic_from_template(
                DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE, 3
            ),
            "/uav3/octomap_point_cloud_centers/reduce_the_frequency",
        )
        collector = RosbridgePointCloudCollector(
            root=Path(tempfile.gettempdir()),
            hosts={1: "127.0.0.1", 2: "127.0.0.1"},
            source_mode=POINTCLOUD_SOURCE_GROUNDSTATION_RELAY,
            default_topic_template=DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
        )
        self.assertEqual(
            collector.topic_for(2),
            "/uav2/octomap_point_cloud_centers/reduce_the_frequency",
        )
        self.assertEqual(
            collector.status()["source_mode"], POINTCLOUD_SOURCE_GROUNDSTATION_RELAY
        )

    def test_groundstation_shared_ingest_stores_without_websocket(self):
        raw = struct.pack("<fff", 1.0, 2.0, 3.0)
        payload = {
            "op": "publish",
            "topic": "/uav3/octomap_point_cloud_centers/reduce_the_frequency",
            "msg": {
                "header": {"frame_id": "map", "stamp": {"secs": 1, "nsecs": 0}},
                "height": 1, "width": 1, "point_step": 12,
                "fields": [
                    {"name": "x", "offset": 0, "datatype": 7, "count": 1},
                    {"name": "y", "offset": 4, "datatype": 7, "count": 1},
                    {"name": "z", "offset": 8, "datatype": 7, "count": 1},
                ],
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            collector = RosbridgePointCloudCollector(
                root=Path(directory), hosts={3: "groundstation-shared-ingress"},
                source_mode=POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
                default_topic_template=DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
            )
            collector.start()
            self.assertEqual(collector._threads, [])
            collector.ingest_rosbridge_payload(3, payload)
            self.assertEqual(collector.latest(3)["points"], [[1.0, 2.0, 3.0]])
            self.assertEqual(collector.status()["message_count"], {"3": 1})

    def test_groundstation_shared_accepts_compressed_transport_topic(self):
        raw = struct.pack("<fff", 1.0, 2.0, 3.0)
        payload = {
            "op": "publish",
            "topic": "/uav3/octomap_point_cloud_centers/reduce_the_frequency/compressed",
            "msg": {
                "header": {"frame_id": "map", "stamp": {"secs": 1, "nsecs": 0}},
                "height": 1, "width": 1, "point_step": 12,
                "fields": [
                    {"name": "x", "offset": 0, "datatype": 7, "count": 1},
                    {"name": "y", "offset": 4, "datatype": 7, "count": 1},
                    {"name": "z", "offset": 8, "datatype": 7, "count": 1},
                ],
                "data": base64.b64encode(raw).decode("ascii"),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            collector = RosbridgePointCloudCollector(
                root=Path(directory), hosts={3: "groundstation-shared-ingress"},
                source_mode=POINTCLOUD_SOURCE_GROUNDSTATION_SHARED,
                default_topic_template=DEFAULT_GROUNDSTATION_RELAY_TOPIC_TEMPLATE,
            )
            collector.ingest_rosbridge_payload(3, payload)
            self.assertEqual(collector.latest(3)["points"], [[1.0, 2.0, 3.0]])

    def test_parse_hosts_topics_and_store_pcd(self):
        self.assertEqual(parse_pointcloud_hosts("1=192.168.1.88;2=ws://10.0.0.2:9090/"), {1: "192.168.1.88", 2: "ws://10.0.0.2:9090/"})
        self.assertEqual(parse_pointcloud_topics("1=/uav1/mid_point_cloud_centers"), {1: "/uav1/mid_point_cloud_centers"})
        with tempfile.TemporaryDirectory() as directory:
            collector = RosbridgePointCloudCollector(
                root=Path(directory),
                hosts={1: "127.0.0.1"},
                max_frames_per_uav=2,
            )
            frame = PointCloudFrame(
                uav_id=1,
                topic="/uav1/mid_point_cloud_centers",
                received_at=1.0,
                points=[[1.0, 2.0, 3.0]],
                frame_id="world",
                stamp=1.0,
                source_points=1,
            )
            path = collector._save_pcd(frame)
            self.assertTrue(path.is_file())
            self.assertIn(b"DATA binary", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
