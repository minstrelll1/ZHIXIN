import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from competition_backend.image_aggregation import (
    PeerImageCollector,
    local_image_manifest,
    resolve_image_file,
    sha256_file,
)


class ImageAggregationTest(unittest.TestCase):
    def test_manifest_is_scoped_to_one_uav_and_contains_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "UAV2" / "mission-a" / "target.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"image-data")
            other = root / "UAV3" / "mission-a" / "other.jpg"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"must-not-leak")

            result = local_image_manifest(root, 2)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["relative_path"], "UAV2/mission-a/target.jpg")
            self.assertEqual(
                result[0]["sha256"], hashlib.sha256(b"image-data").hexdigest()
            )
            self.assertEqual(
                resolve_image_file(root, 2, result[0]["relative_path"]),
                path.resolve(),
            )
            with self.assertRaises(ValueError):
                resolve_image_file(root, 2, "UAV3/mission-a/other.jpg")

    def test_manifest_reuses_hash_until_same_size_file_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "UAV2" / "subject1-live" / "target.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"old-data")
            with mock.patch("competition_backend.image_aggregation.sha256_file", wraps=sha256_file) as hasher:
                old = local_image_manifest(root, 2)[0]["sha256"]
                self.assertEqual(old, local_image_manifest(root, 2)[0]["sha256"])
                self.assertEqual(1, hasher.call_count)
                replacement = path.with_name("replacement.part")
                replacement.write_bytes(b"new-data")
                previous_ns = path.stat().st_mtime_ns
                os.utime(replacement, ns=(previous_ns + 1_000_000_000, previous_ns + 1_000_000_000))
                os.replace(replacement, path)
                new = local_image_manifest(root, 2)[0]["sha256"]
                self.assertNotEqual(old, new)
                self.assertEqual(2, hasher.call_count)

    def test_publisher_collects_new_peer_result_and_rebuilds_json_without_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_root = root / "remote"
            local_root = root / "publisher"
            peer_mission = remote_root / "UAV2" / "subject1-live"
            peer_mission.mkdir(parents=True)
            metadata = {
                "mission_id": "subject1-live", "target_id": "target-2",
                "target_type": "车辆", "target_model": "车辆2",
                "confidence": 0.91, "target_latitude": 39.05,
                "target_longitude": 121.66,
                "image_stamp": {"secs": 1_700_000_000, "nsecs": 0},
            }
            (peer_mission / "target.json").write_text(json.dumps(metadata), encoding="utf-8")
            (peer_mission / "target.jpg").write_bytes(b"test-jpeg")

            class PeerHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed = urllib.parse.urlparse(self.path)
                    query = urllib.parse.parse_qs(parsed.query)
                    if self.headers.get("X-Competition-Peer-Token") != "peer-secret":
                        self.send_error(403)
                        return
                    if parsed.path.endswith("/manifest"):
                        raw = json.dumps({"files": local_image_manifest(remote_root, 2)}).encode()
                    elif parsed.path.endswith("/file"):
                        raw = resolve_image_file(remote_root, 2, query["relative_path"][0]).read_bytes()
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), PeerHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            role = {"publisher": False}
            collector = PeerImageCollector(
                local_root, 1, {2: "http://127.0.0.1:%d" % server.server_port},
                "peer-secret", interval_sec=1.0,
                should_collect=lambda: role["publisher"],
            )
            try:
                collector.start()  # 不调用 activate；角色选定后应自行开始。
                time.sleep(0.15)
                self.assertFalse((local_root / "UAV2").exists())
                role["publisher"] = True
                output = local_root / "subject1_submissions" / "subject1-live" / "target-submission.json"
                deadline = time.monotonic() + 5
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(output.exists(), collector.status())
                self.assertEqual((local_root / "UAV2/subject1-live/target.jpg").read_bytes(), b"test-jpeg")
                self.assertEqual([], list((local_root / "UAV2/subject1-live").glob("*.part-*")))
                document = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(["target-2"], [item["id"] for item in document["features"]])
                self.assertTrue((output.parent / "images").exists())
                status = collector.status()
                self.assertTrue(status["active"])
                self.assertEqual(2, status["downloaded_count"])
                self.assertGreaterEqual(status["aggregated_count"], 1)
                self.assertIsNotNone(status["peers"]["2"]["last_success_at"])
                later = dict(metadata, target_id="target-3", target_longitude=121.661,
                             image_stamp={"secs": 1_700_000_001, "nsecs": 0})
                (peer_mission / "later.json").write_text(json.dumps(later), encoding="utf-8")
                (peer_mission / "later.jpg").write_bytes(b"second-jpeg")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    document = json.loads(output.read_text(encoding="utf-8"))
                    if {item["id"] for item in document["features"]} == {"target-2", "target-3"}:
                        break
                    time.sleep(0.05)
                self.assertEqual({"target-2", "target-3"},
                                 {item["id"] for item in document["features"]})
                self.assertEqual(4, collector.status()["downloaded_count"])
                from competition_backend.subject1_reporting import Subject1Reporter, TEAM_NAME
                reporter = Subject1Reporter(local_root)
                prepared = reporter.prepare(reporter.build("subject1-live", TEAM_NAME))
                self.assertEqual(prepared["target_count"], 2)
                self.assertTrue(reporter.draft(prepared["draft_id"]).is_file())
                role["publisher"] = False
                self.assertFalse(collector.status()["active"])
            finally:
                collector.stop()
                server.shutdown()
                server.server_close()

    def test_slow_peer_does_not_block_another_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fast_finished = threading.Event()
            release_slow = threading.Event()

            class SimulatedCollector(PeerImageCollector):
                def _sync_peer(self, uav_id, base_url):
                    if uav_id == 2:
                        release_slow.wait(3)
                    else:
                        fast_finished.set()

            collector = SimulatedCollector(root, 1, {2: "slow", 3: "fast"}, "token",
                                           should_collect=lambda: True)
            try:
                collector.start()
                self.assertTrue(fast_finished.wait(1.5), "慢对端阻塞了另一台无人机的结果核对")
            finally:
                release_slow.set()
                collector.stop()


if __name__ == "__main__":
    unittest.main()
