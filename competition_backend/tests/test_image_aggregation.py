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
    def test_manifest_recent_filter_prunes_old_missions_and_mission_filter_restores_all_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "UAV2" / "subject1-old"
            current = root / "UAV2" / "subject1-current"
            old.mkdir(parents=True)
            current.mkdir(parents=True)
            old_file = old / "first.jpg"
            old_file.write_bytes(b"old")
            older_current_file = current / "first.jpg"
            older_current_file.write_bytes(b"earlier target in active mission")
            recent_file = current / "second.jpg"
            recent_file.write_bytes(b"latest target")
            stale = time.time() - 3600
            for path in (old_file, older_current_file, old):
                os.utime(path, (stale, stale))

            with mock.patch("competition_backend.image_aggregation.sha256_file", wraps=sha256_file) as hasher:
                recent = local_image_manifest(root, 2, max_age_seconds=120)
                self.assertEqual(["UAV2/subject1-current/second.jpg"],
                                 [item["relative_path"] for item in recent])
                self.assertEqual(1, hasher.call_count)
            full_current = local_image_manifest(root, 2, mission_id="subject1-current")
            self.assertEqual({"UAV2/subject1-current/first.jpg", "UAV2/subject1-current/second.jpg"},
                             {item["relative_path"] for item in full_current})
            self.assertEqual(3, len(local_image_manifest(root, 2)))

    def test_manifest_filter_arguments_are_bounded_and_paths_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for invalid_age in (-1, float("nan"), float("inf"), 8 * 86400):
                with self.subTest(max_age_seconds=invalid_age), self.assertRaises(ValueError):
                    local_image_manifest(root, 2, max_age_seconds=invalid_age)
            for invalid_mission in ("../outside", "..", "subject1/current", "subject1\\current", ""):
                with self.subTest(mission_id=invalid_mission), self.assertRaises(ValueError):
                    local_image_manifest(root, 2, mission_id=invalid_mission)

    def test_peer_manifest_endpoint_exposes_filter_status_and_rejects_unsafe_values(self):
        from fastapi.testclient import TestClient
        from competition_backend.api import create_app
        from competition_shared.fleet import apply_fixed_binding, default_fleet

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_root = root / "images"
            image = image_root / "UAV2" / "subject1-active" / "target.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"test")
            fleet = default_fleet()
            for uav_id in range(1, 7):
                fleet = apply_fixed_binding(fleet, uav_id, "p600")
            config_path = root / "fleet.json"
            config_path.write_text(json.dumps(fleet), encoding="utf-8")
            app = create_app({
                "COMPETITION_ADAPTER": "distributed",
                "COMPETITION_GROUND_TERMINAL_ID": "2",
                "COMPETITION_FLEET_CONFIG": str(config_path),
                "COMPETITION_DATA_DIR": str(root / "data"),
                "COMPETITION_DIAGNOSTICS_DIR": str(root / "diagnostics"),
                "COMPETITION_IMAGE_ROOT": str(image_root),
                "COMPETITION_PEER_TOKEN": "peer-secret",
            })
            client = TestClient(app)
            headers = {"X-Competition-Peer-Token": "peer-secret"}
            endpoint = "/api/v1/peer/images/manifest?uav_id=2"
            plain = client.get(endpoint, headers=headers)
            self.assertEqual(200, plain.status_code, plain.text)
            self.assertFalse(plain.json()["filter_applied"])
            self.assertEqual(1, len(plain.json()["files"]))
            recent = client.get(endpoint + "&max_age_seconds=60", headers=headers)
            self.assertEqual(200, recent.status_code, recent.text)
            self.assertTrue(recent.json()["filter_applied"])
            selected = client.get(endpoint + "&mission_id=subject1-active", headers=headers)
            self.assertEqual(200, selected.status_code, selected.text)
            self.assertTrue(selected.json()["filter_applied"])
            self.assertEqual(1, len(selected.json()["files"]))
            self.assertEqual(422, client.get(endpoint + "&max_age_seconds=nan", headers=headers).status_code)
            self.assertEqual(422, client.get(endpoint + "&mission_id=..%2Fescape", headers=headers).status_code)
            self.assertEqual(403, client.get(endpoint).status_code)

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
                "localization_time": {"secs": 1_700_000_000, "nsecs": 0},
            }
            (peer_mission / "target.json").write_text(json.dumps(metadata), encoding="utf-8")
            (peer_mission / "target.jpg").write_bytes(b"test-jpeg")
            earlier = dict(metadata, target_id="target-earlier", target_longitude=121.662,
                           localization_time={"secs": 1_699_999_990, "nsecs": 0})
            (peer_mission / "earlier.json").write_text(json.dumps(earlier), encoding="utf-8")
            (peer_mission / "earlier.jpg").write_bytes(b"earlier-jpeg")
            old_time = time.time() - 3600
            for path in peer_mission.glob("earlier.*"):
                os.utime(path, (old_time, old_time))
            old_mission = remote_root / "UAV2" / "subject1-previous-run"
            old_mission.mkdir(parents=True)
            (old_mission / "old.json").write_text(
                json.dumps(dict(metadata, mission_id="subject1-previous-run")), encoding="utf-8")
            (old_mission / "old.jpg").write_bytes(b"previous-run")
            for path in old_mission.iterdir():
                os.utime(path, (old_time, old_time))
            os.utime(old_mission, (old_time, old_time))
            other_subject = remote_root / "UAV2" / "subject2-new"
            other_subject.mkdir(parents=True)
            (other_subject / "unrelated.json").write_text("{}", encoding="utf-8")

            class PeerHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    parsed = urllib.parse.urlparse(self.path)
                    query = urllib.parse.parse_qs(parsed.query)
                    if self.headers.get("X-Competition-Peer-Token") != "peer-secret":
                        self.send_error(403)
                        return
                    if parsed.path.endswith("/manifest"):
                        age = query.get("max_age_seconds", [None])[0]
                        mission = query.get("mission_id", [None])[0]
                        raw = json.dumps({
                            "files": local_image_manifest(remote_root, 2,
                                                          max_age_seconds=float(age) if age else None,
                                                          mission_id=mission),
                            "filter_applied": age is not None,
                        }).encode()
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
                session_started_monotonic=time.monotonic(),
            )
            try:
                collector.start()  # 不调用 activate；角色选定后应自行开始。
                time.sleep(0.15)
                self.assertFalse((local_root / "UAV2").exists())
                role["publisher"] = True
                output = local_root / "subject1_submissions" / "subject1-live" / "target-submission.json"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if output.exists():
                        ids = {item["id"] for item in json.loads(output.read_text(encoding="utf-8"))["features"]}
                        if ids == {"target-001", "target-002"}:
                            break
                    time.sleep(0.05)
                self.assertTrue(output.exists(), collector.status())
                self.assertEqual((local_root / "UAV2/subject1-live/target.jpg").read_bytes(), b"test-jpeg")
                self.assertEqual((local_root / "UAV2/subject1-live/earlier.jpg").read_bytes(), b"earlier-jpeg")
                self.assertFalse((local_root / "UAV2/subject1-previous-run").exists())
                self.assertFalse((local_root / "UAV2/subject2-new").exists())
                self.assertEqual([], list((local_root / "UAV2/subject1-live").glob("*.part-*")))
                document = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual({"target-001", "target-002"},
                                 {item["id"] for item in document["features"]})
                format_audit=json.loads(output.with_name("submission-format.json").read_text(encoding="utf-8"))
                self.assertEqual({"target-2","target-earlier"}, {entry["source_id"] for entry in format_audit["id_mapping"]})
                self.assertTrue((output.parent / "images").exists())
                status = collector.status()
                self.assertTrue(status["active"])
                self.assertEqual(4, status["downloaded_count"])
                self.assertGreaterEqual(status["aggregated_count"], 1)
                self.assertIsNotNone(status["peers"]["2"]["last_success_at"])
                later = dict(metadata, target_id="target-3", target_longitude=121.661,
                             localization_time={"secs": 1_700_000_001, "nsecs": 0})
                (peer_mission / "later.json").write_text(json.dumps(later), encoding="utf-8")
                (peer_mission / "later.jpg").write_bytes(b"second-jpeg")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    document = json.loads(output.read_text(encoding="utf-8"))
                    if {item["id"] for item in document["features"]} == {"target-001", "target-002", "target-003"}:
                        break
                    time.sleep(0.05)
                self.assertEqual({"target-001", "target-002", "target-003"},
                                 {item["id"] for item in document["features"]})
                self.assertEqual(6, collector.status()["downloaded_count"])
                from competition_backend.subject1_reporting import Subject1Reporter, TEAM_NAME
                reporter = Subject1Reporter(local_root)
                prepared = reporter.prepare(reporter.build("subject1-live", TEAM_NAME))
                self.assertEqual(prepared["target_count"], 3)
                self.assertTrue(reporter.draft(prepared["draft_id"]).is_file())
                role["publisher"] = False
                self.assertFalse(collector.status()["active"])
            finally:
                collector.stop()
                server.shutdown()
                server.server_close()

    def test_json_arrives_at_publisher_before_a_failed_peer_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote_root = root / "remote"
            local_root = root / "publisher"
            mission = remote_root / "UAV2" / "subject1-json-priority"
            mission.mkdir(parents=True)
            metadata = dict(mission_id="subject1-json-priority", target_id="fixed-2",
                            target_type="车辆", target_model="车辆1", is_moving=False,
                            target_latitude=34.1, target_longitude=113.9, confidence=.8,
                            localization_time=dict(secs=1_700_000_000, nsecs=0))
            (mission / "target.json").write_text(json.dumps(metadata), encoding="utf-8")
            (mission / "target.jpg").write_bytes(b"large-image-placeholder")

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
                        if query["relative_path"][0].endswith(".jpg"):
                            self.send_error(503, "image temporarily unavailable")
                            return
                        raw = resolve_image_file(remote_root, 2, query["relative_path"][0]).read_bytes()
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

                def log_message(self, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), PeerHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:%d" % server.server_port
            collector = PeerImageCollector(local_root, 1, {2: url}, "peer-secret",
                                           publisher_dedup=True)
            try:
                with self.assertRaises(Exception):
                    collector._sync_peer(2, url)
                self.assertTrue((local_root / "UAV2/subject1-json-priority/target.json").is_file())
                self.assertFalse((local_root / "UAV2/subject1-json-priority/target.jpg").exists())
                result = collector._rebuild_subject1("subject1-json-priority")
                document = json.loads(result.read_text(encoding="utf-8"))
                self.assertEqual(["target-001"], [item["id"] for item in document["features"]])
                format_audit=json.loads(result.with_name("submission-format.json").read_text(encoding="utf-8"))
                self.assertEqual("fixed-2",format_audit["id_mapping"][0]["source_id"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_two_peers_moving_results_prepare_and_upload_selected_forty_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publisher = root / "publisher"
            servers = []

            def open_peer(uav_id, count, confidence):
                remote = root / ("ground-%d" % uav_id)
                mission = remote / ("UAV%d" % uav_id) / "subject1-multi-ground"
                mission.mkdir(parents=True)
                for index in range(count):
                    record = dict(mission_id="subject1-multi-ground",
                                  target_id="moving-%d" % uav_id, target_type="车辆",
                                  target_model="车辆1", is_moving=True,
                                  target_latitude=34.1,
                                  target_longitude=113.9 + index * .00005,
                                  confidence=confidence,
                                  localization_time=dict(secs=1_700_000_000 + index * 3, nsecs=0))
                    (mission / ("point-%03d.json" % index)).write_text(
                        json.dumps(record), encoding="utf-8")

                class PeerHandler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        parsed = urllib.parse.urlparse(self.path)
                        query = urllib.parse.parse_qs(parsed.query)
                        if self.headers.get("X-Competition-Peer-Token") != "peer-secret":
                            self.send_error(403)
                            return
                        if parsed.path.endswith("/manifest"):
                            raw = json.dumps({"files": local_image_manifest(remote, uav_id)}).encode()
                        elif parsed.path.endswith("/file"):
                            raw = resolve_image_file(remote, uav_id,
                                                     query["relative_path"][0]).read_bytes()
                        else:
                            self.send_error(404)
                            return
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)

                    def log_message(self, *_args):
                        pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), PeerHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append((server, thread))
                return "http://127.0.0.1:%d" % server.server_port

            try:
                peer2 = open_peer(2, 6, .95)
                peer3 = open_peer(3, 43, .40)
                collector = PeerImageCollector(publisher, 1, {2: peer2, 3: peer3},
                                               "peer-secret", publisher_dedup=True)
                collector._sync_peer(2, peer2)
                collector._sync_peer(3, peer3)
                self.assertEqual(49, len(list(publisher.glob("UAV*/*/*.json"))))
                result = collector._rebuild_subject1("subject1-multi-ground")
                document = json.loads(result.read_text(encoding="utf-8"))
                feature, = document["features"]
                self.assertEqual("target-001", feature["id"])
                format_audit=json.loads(result.with_name("submission-format.json").read_text(encoding="utf-8"))
                self.assertEqual("moving-3",format_audit["id_mapping"][0]["source_id"])
                self.assertEqual(40, len(feature["properties"]["trackPoints"]))
                from competition_backend.subject1_reporting import Subject1Reporter, TEAM_NAME
                reporter = Subject1Reporter(publisher, publisher_dedup=True)
                prepared = reporter.prepare(document, already_deduplicated=True)
                sent = []

                class Response:
                    code = 200

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False

                    def read(self, _size):
                        return b'{"accepted":true}'

                class Opener:
                    def open(self, request, timeout):
                        sent.append((request, timeout))
                        return Response()

                with mock.patch("competition_backend.subject1_reporting.urllib.request.build_opener",
                                return_value=Opener()):
                    receipt = reporter.submit(prepared["draft_id"])
                self.assertEqual("http_received", receipt["state"])
                self.assertEqual(1, len(sent))
                self.assertIn(b'name="file"; filename="target-submission.json"', sent[0][0].data)
                self.assertIn(b'"trackPoints"', sent[0][0].data)
            finally:
                for server, thread in servers:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

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
