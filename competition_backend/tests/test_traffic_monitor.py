import socket
import unittest
from unittest.mock import patch

from competition_backend.traffic_monitor import (
    TcpCounters,
    TrafficMonitor,
    _port_from_uint32,
    classify_traffic_category,
    parse_uav_traffic_hosts,
)


class TrafficMonitorTest(unittest.TestCase):
    def test_parse_uav_traffic_hosts(self):
        self.assertEqual(
            parse_uav_traffic_hosts("1=192.168.1.88;3=192.168.1.90"),
            {1: "192.168.1.88", 3: "192.168.1.90"},
        )

    def test_parse_uav_traffic_hosts_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            parse_uav_traffic_hosts("3=not-an-ip")
        with self.assertRaises(ValueError):
            parse_uav_traffic_hosts("9=192.168.1.88")

    def test_converts_windows_network_byte_order_port(self):
        self.assertEqual(_port_from_uint32(socket.htons(56100)), 56100)

    def test_classifies_media_and_prometheus_channels(self):
        self.assertEqual(classify_traffic_category(5000, 1234), "gimbal_video")
        self.assertEqual(classify_traffic_category(56010, 5000), "image_return")
        self.assertEqual(classify_traffic_category(5000, 9090), "rviz_pointcloud")
        self.assertEqual(classify_traffic_category(5000, 55556), "prometheus")

    def test_reported_relay_bytes_are_included_in_category_and_total(self):
        monitor = TrafficMonitor({3: "192.168.1.88"})
        monitor._previous_at = 100.0
        monitor.record_application_bytes(
            3, "rviz_pointcloud", received_bytes=125000
        )
        with patch(
            "competition_backend.traffic_monitor._windows_tcp_estats",
            return_value=({}, "No active TCP connection to the configured onboard computer"),
        ), patch(
            "competition_backend.traffic_monitor.time.time",
            return_value=101.0,
        ):
            monitor._sample()
        status = monitor.status_for_uav(3)
        self.assertEqual(status["total_bytes_per_sec"], 125000)
        pointcloud = next(
            row for row in status["categories"] if row["category"] == "rviz_pointcloud"
        )
        self.assertEqual(pointcloud["speed_mbps"], 1.0)

    def test_video_endpoint_is_assigned_to_the_uav(self):
        monitor = TrafficMonitor(
            {3: "192.168.1.88"}, video_hosts={3: "192.168.1.99"}
        )
        monitor._previous_at = 100.0
        with patch(
            "competition_backend.traffic_monitor._windows_tcp_estats",
            return_value=(
                {
                    "192.168.1.99": {
                        (51000, 1234, 7): TcpCounters(100, 200)
                    }
                },
                "",
            ),
        ), patch(
            "competition_backend.traffic_monitor.time.time",
            return_value=101.0,
        ):
            monitor._sample()
        video = next(
            row
            for row in monitor.status_for_uav(3)["categories"]
            if row["category"] == "gimbal_video"
        )
        self.assertEqual(video["speed_mbps"], 0.0024)

    def test_detects_duplicate_pointcloud_connections(self):
        monitor = TrafficMonitor({3: "192.168.1.88"})
        monitor._previous_at = 100.0
        with patch(
            "competition_backend.traffic_monitor._windows_tcp_estats",
            return_value=(
                {
                    "192.168.1.88": {
                        (51000, 9090, 101): TcpCounters(100, 200),
                        (51001, 9090, 102): TcpCounters(300, 400),
                    }
                },
                "",
            ),
        ), patch(
            "competition_backend.traffic_monitor.time.time",
            return_value=101.0,
        ):
            monitor._sample()
        status = monitor.status_for_uav(3)
        self.assertEqual(len(status["duplicate_links"]), 1)
        self.assertEqual(status["duplicate_links"][0]["category"], "rviz_pointcloud")
        self.assertEqual(status["duplicate_links"][0]["connection_count"], 2)

    def test_configured_link_plan_is_exposed(self):
        monitor = TrafficMonitor({3: "192.168.1.88"})
        monitor.set_link_plan(
            {3: [{"id": "pointcloud", "category": "rviz_pointcloud", "endpoint": "192.168.1.88:9090"}]}
        )
        self.assertEqual(
            monitor.status_for_uav(3)["configured_links"][0]["endpoint"],
            "192.168.1.88:9090",
        )

    def test_accumulates_only_configured_uav_connection_deltas(self):
        monitor = TrafficMonitor({3: "192.168.1.88"})
        monitor._previous_at = 100.0
        samples = [
            (
                {
                    "192.168.1.88": {
                        (5000, 56100, 100): TcpCounters(10, 20),
                    }
                },
                "",
            ),
            (
                {
                    "192.168.1.88": {
                        (5000, 56100, 100): TcpCounters(17, 27),
                    }
                },
                "",
            ),
        ]
        with patch(
            "competition_backend.traffic_monitor._windows_tcp_estats",
            side_effect=samples,
        ), patch(
            "competition_backend.traffic_monitor.time.time",
            side_effect=[101.0, 102.0],
        ):
            monitor._sample()
            monitor._sample()

        status = monitor.status_for_uav(3)
        self.assertEqual(status["sent_bytes"], 17)
        self.assertEqual(status["received_bytes"], 27)
        self.assertEqual(status["total_bytes"], 44)
        self.assertEqual(status["sent_bytes_per_sec"], 7)
        self.assertEqual(status["received_bytes_per_sec"], 7)
        self.assertEqual(
            status["ports"],
            [
                {
                    "port": 56100,
                    "local_port": 5000,
                    "remote_port": 56100,
                    "function": "地面端任务/遥测",
                    "speed_mbps": 0.000112,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
