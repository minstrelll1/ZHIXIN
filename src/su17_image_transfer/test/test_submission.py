import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from unittest.mock import patch

from su17_image_transfer.submission import update_subject1_submission, _iso_from_metadata


class SubmissionTest(unittest.TestCase):
    def test_localization_time_for_both_static_and_moving_in_beijing(self):
        import datetime
        data = dict(image_stamp=dict(secs=1700000000,nsecs=123456789),
                    target_timestamp=dict(secs=1700000001,nsecs=0),
                    localization_time=dict(secs=1700000003,nsecs=456789123),
                    selected_image_stamp=dict(secs=1700000099,nsecs=0),
                    requested_at_unix_ns=1800000000000000000,
                    source_stamp_sec=1700000099,source_stamp_nsec=0)
        for moving in (False, True):
            with self.subTest(moving=moving):
                text = _iso_from_metadata(dict(data,is_moving=moving))
                self.assertEqual("2023-11-15T06:13:23.456+08:00", text)
                self.assertAlmostEqual(1700000003.456, datetime.datetime.fromisoformat(text).timestamp(), places=3)
                for stamp in (dict(secs=0,nsecs=0),dict(secs=1700000000,nsecs=1000000000),
                              dict(secs=True,nsecs=0),dict(secs=-1,nsecs=0),None,
                              dict(secs=1400000,nsecs=0),dict(secs=4102444800,nsecs=0)):
                    with self.subTest(stamp=stamp):
                        self.assertIsNone(_iso_from_metadata(dict(data,is_moving=moving,localization_time=stamp)))
        del data['localization_time']
        self.assertIsNone(_iso_from_metadata(data))

    def test_final_format_keeps_source_audit_and_excludes_missing_source_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); mission = "subject1-format"
            folder = root/"UAV5"/mission; folder.mkdir(parents=True)
            for i in range(3):
                metadata = dict(mission_id=mission,target_id="moving-person" if i<2 else "missing-time",
                                category_id=18,is_moving=i<2,longitude_deg=113.+i*.0001,
                                latitude_deg=34.,localization_time=dict(secs=1700000000+i,nsecs=0) if i<2 else None,
                                selected_image_stamp=dict(secs=1700000999,nsecs=0),
                                requested_at_unix_ns=1800000000000000000)
                (folder/(str(i)+".json")).write_text(json.dumps(metadata),encoding="utf-8")
            output=update_subject1_submission(root,mission,publisher_dedup=True)
            document=json.loads(output.read_text(encoding="utf-8")); feature,=document["features"]
            self.assertEqual("target-001",feature["id"])
            self.assertEqual("人员4",feature["properties"]["targetModel"])
            self.assertEqual("移动",feature["properties"]["targetCategory"])
            self.assertEqual("2023-11-15T06:13:20.000+08:00",feature["properties"]["trackStartTime"])
            self.assertNotIn("indoorTargets",document["metadata"])
            audit=json.loads(output.with_name("submission-format.json").read_text(encoding="utf-8"))
            self.assertEqual([dict(source_id="moving-person",submission_id="target-001")],audit["id_mapping"])
            self.assertEqual("missing-time",audit["excluded_targets"][0]["id"])
            self.assertIn("源时间戳",audit["excluded_targets"][0]["reason"])
            self.assertEqual(3,len(list(folder.glob("*.json"))))
            self.assertEqual("localization_time", audit["target_time"]["source"])
            self.assertEqual("missing-time", audit["target_time"]["invalid_records"][0]["target_id"])

    def test_publisher_receiver_does_not_overwrite_deduplicated_submission(self):
        package_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "publisher_receiver_test", package_root / "ground" / "ground_image_receiver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            receiver = module.GroundImageReceiver("127.0.0.1", 0, Path(directory), "",
                                                   status_interval=0, publisher_dedup=True)
            try:
                mission_id = "subject1-publisher-receiver"
                for uav_id, longitude, confidence in ((1, 121.660000, .4), (2, 121.660050, .9)):
                    receiver._save_image(dict(uav_id=uav_id, mission_id=mission_id,
                                              request_id="target-%d" % uav_id,
                                              target_type="车辆", target_model="车辆1",
                                              target_latitude=39.05, target_longitude=longitude,
                                              confidence=confidence,
                                              localization_time=dict(secs=1_700_000_000, nsecs=0)),
                                         b"image-%d" % uav_id)
                result = Path(directory) / "subject1_submissions" / mission_id / "target-submission.json"
                decisions = result.parent / "dedup-decisions.json"
                deadline = time.monotonic() + 4
                while (not decisions.is_file() or not json.loads(decisions.read_text(encoding="utf-8"))["merged"]) \
                        and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(decisions.is_file())
                self.assertEqual(1, len(json.loads(decisions.read_text(encoding="utf-8"))["merged"]))
                self.assertEqual(1, len(json.loads(result.read_text(encoding="utf-8"))["features"]))
                self.assertEqual(2, len(list(Path(directory).glob("UAV*/*/*.json"))))
            finally:
                receiver.stop()

    def test_publisher_merges_confused_cross_uav_targets_and_keeps_raw_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-cross-uav"

            def add(uid, name, target_id, model, lon, confidence, moving=False, reverse=False):
                folder = root / ("UAV%d" % uid) / mission_id
                folder.mkdir(parents=True, exist_ok=True)
                count = 2 if moving else 1
                for index in range(count):
                    longitude = lon + ((1 - index) if reverse else index) * 0.0001 if moving else lon
                    payload = dict(mission_id=mission_id, target_id=target_id,
                                   target_type="车辆", target_model=model,
                                   is_moving=moving, target_latitude=39.05,
                                   target_longitude=longitude, confidence=confidence,
                                   localization_time=dict(secs=1_700_000_000 + index * 10, nsecs=0))
                    stem = "%s-%d" % (name, index)
                    (folder / (stem + ".json")).write_text(json.dumps(payload), encoding="utf-8")
                    (folder / (stem + ".jpg")).write_bytes(("image-" + name).encode())

            add(1, "fixed-low", "same-id", "车辆1", 121.660000, .42)
            add(2, "fixed-high", "other-id", "车辆1", 121.660050, .91)
            add(3, "far-same-id", "same-id", "车辆1", 121.660500, .8)
            add(4, "other-model", "another-id", "车辆2", 121.660052, .75)
            add(1, "moving-a", "moving-1", "车辆3", 121.661000, .55, moving=True)
            add(2, "moving-b", "moving-2", "车辆3", 121.661045, .88, moving=True)
            add(3, "moving-opposite", "moving-3", "车辆3", 121.661000, .72, moving=True, reverse=True)

            path = update_subject1_submission(root, mission_id, publisher_dedup=True)
            document = json.loads(path.read_text(encoding="utf-8"))
            from competition_backend.subject1_reporting import validate_document
            self.assertEqual(4, validate_document(document)["target_count"])
            features = document["features"]
            self.assertEqual(sorted((f["properties"]["confidence"] for f in features), reverse=True),
                             [f["properties"]["confidence"] for f in features])
            self.assertEqual(len({f["id"] for f in features}), 4)
            self.assertEqual("target-001", features[0]["id"])
            self.assertEqual(b"image-fixed-high", (path.parent / features[0]["properties"]["imagePath"]).read_bytes())
            self.assertEqual(2, len([f for f in features if f["properties"]["targetCategory"] == "固定"]))
            self.assertEqual(2, len([f for f in features if f["properties"]["targetCategory"] == "移动"]))
            decisions = json.loads((path.parent / "dedup-decisions.json").read_text(encoding="utf-8"))
            self.assertEqual(3, len(decisions["merged"]))
            self.assertEqual(7, len(json.loads((path.parent / "raw-targets.json").read_text(encoding="utf-8"))["features"]))
            self.assertEqual(10, len(list(root.glob("UAV*/*/*.json"))))

    def test_clock_offset_dedup_does_not_interleave_uncalibrated_moving_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-clock-offset"
            start = 1_700_000_000
            for uav_id, target_id, offset, confidence in (
                (1, "moving-low", 0, .5),
                (2, "moving-high", 3, .9),
            ):
                folder = root / ("UAV%d" % uav_id) / mission_id
                folder.mkdir(parents=True)
                for index in range(11):
                    metadata = dict(
                        mission_id=mission_id, target_id=target_id,
                        target_type="车辆", target_model="车辆1", is_moving=True,
                        target_latitude=39.05,
                        target_longitude=121.66 + index * .000058,
                        confidence=confidence,
                        localization_time=dict(secs=start + index + offset, nsecs=0),
                    )
                    stem = "frame-%02d" % index
                    (folder / (stem + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                    (folder / (stem + ".jpg")).write_bytes(b"jpeg")

            path = update_subject1_submission(root, mission_id, publisher_dedup=True)
            document = json.loads(path.read_text(encoding="utf-8"))
            from competition_backend.subject1_reporting import validate_document
            self.assertEqual(1, validate_document(document)["target_count"])
            feature = document["features"][0]
            self.assertEqual("target-001", feature["id"])
            self.assertEqual(11, len(feature["properties"]["trackPoints"]))
            self.assertEqual("2023-11-15T06:13:23.000+08:00", feature["properties"]["trackStartTime"])
            longitudes = [point[0] for point in feature["geometry"]["coordinates"]]
            self.assertEqual(sorted(longitudes), longitudes)
            decisions = json.loads((path.parent / "dedup-decisions.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(decisions["merged"]))
            self.assertNotEqual(0, decisions["merged"][0]["time_offset_seconds"])
            self.assertFalse(decisions["merged"][0]["track_points_merged"])

    def test_multi_uav_moving_target_chooses_longest_single_track(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-moving-longest"
            for uav_id, count, confidence in ((1, 29, .95), (2, 35, .42)):
                folder = root / ("UAV%d" % uav_id) / mission_id
                folder.mkdir(parents=True)
                for index in range(count):
                    payload = dict(mission_id=mission_id, target_id="moving-1",
                                   target_type="车辆", target_model="车辆1", is_moving=True,
                                   target_latitude=34.1,
                                   target_longitude=113.9 + index * .00005,
                                   confidence=confidence,
                                   localization_time=dict(secs=1_700_000_000 + index * 3, nsecs=0))
                    (folder / ("point-%03d.json" % index)).write_text(
                        json.dumps(payload), encoding="utf-8")
            result = update_subject1_submission(root, mission_id, publisher_dedup=True)
            document = json.loads(result.read_text(encoding="utf-8"))
            feature, = document["features"]
            self.assertEqual(35, len(feature["properties"]["trackPoints"]))
            self.assertEqual(.42, feature["properties"]["confidence"])
            self.assertEqual(1, len(json.loads((result.parent / "dedup-decisions.json").read_text(
                encoding="utf-8"))["merged"]))
            from competition_backend.subject1_reporting import validate_document
            self.assertEqual(1, validate_document(document)["moving"])

    def test_moving_track_over_forty_keeps_earliest_forty_points(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-moving-forty"
            for uav_id, count, confidence in ((1, 47, .41), (2, 39, .96)):
                folder = root / ("UAV%d" % uav_id) / mission_id
                folder.mkdir(parents=True)
                for index in range(count):
                    payload = dict(mission_id=mission_id, target_id="moving-1",
                                   target_type="车辆", target_model="车辆1", is_moving=True,
                                   target_latitude=34.1,
                                   target_longitude=113.9 + index * .00005,
                                   confidence=confidence,
                                   localization_time=dict(secs=1_700_000_000 + index * 3, nsecs=0))
                    (folder / ("point-%03d.json" % index)).write_text(
                        json.dumps(payload), encoding="utf-8")
            result = update_subject1_submission(root, mission_id, publisher_dedup=True)
            document = json.loads(result.read_text(encoding="utf-8"))
            feature, = document["features"]
            points = feature["properties"]["trackPoints"]
            self.assertEqual(40, len(points))
            self.assertEqual(.41, feature["properties"]["confidence"])
            self.assertEqual([113.9, 34.1], points[0]["coordinates"])
            self.assertAlmostEqual(113.9 + 39 * .00005, points[-1]["coordinates"][0])
            self.assertEqual(points[-1]["timestamp"], feature["properties"]["trackEndTime"])
            decisions = json.loads((result.parent / "dedup-decisions.json").read_text(encoding="utf-8"))
            self.assertEqual(7, decisions["track_truncations"][0]["omitted_track_points"])
            from competition_backend.subject1_reporting import validate_document
            self.assertEqual(1, validate_document(document)["moving"])

    def test_outdoor_static_and_moving_are_exported_in_template_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV3" / "subject1-run"
            mission.mkdir(parents=True)
            for index, moving in enumerate((False, True, True)):
                name = "target-%s-%d" % ("move" if moving else "fixed", index)
                metadata = {
                    "mission_id": "subject1-run", "target_id": "move" if moving else "fixed",
                    "target_type": "车辆", "target_model": "车辆2", "is_moving": moving,
                    "confidence": 0.9, "target_latitude": 33.86, "target_longitude": 113.70,
                    "target_altitude": 100.0, "localization_time": {"secs": 1_700_000_000 + index, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            target = update_subject1_submission(root, "subject1-run", "测试队")
            document = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual("FeatureCollection", document["type"])
            self.assertEqual("测试队", document["name"])
            self.assertEqual(2, len(document["features"]))
            moving = next(item for item in document["features"] if item["properties"]["targetCategory"] == "移动")
            self.assertEqual("LineString", moving["geometry"]["type"])
            self.assertEqual(2, len(moving["properties"]["trackPoints"]))
            self.assertTrue(any((target.parent / "images").glob("*.jpg")))

    def test_indoor_xyz_is_retained_but_not_written_as_wgs84(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV1" / "subject1-indoor"
            mission.mkdir(parents=True)
            metadata = {
                "mission_id": "subject1-indoor", "target_id": "lab-1", "target_type": "人员",
                "indoor_position": True, "east_m": 1.2, "north_m": 2.3, "up_m": 0.4,
                "localization_time": {"secs": 1_700_000_000, "nsecs": 0},
            }
            (mission / "lab.json").write_text(json.dumps(metadata), encoding="utf-8")
            (mission / "lab.jpg").write_bytes(b"jpeg")
            output = update_subject1_submission(root, "subject1-indoor")
            document = json.loads(output.read_text(encoding="utf-8"))
            excluded = json.loads(output.with_name("submission-format.json").read_text(encoding="utf-8"))["excluded_targets"]
            self.assertEqual([], document["features"])
            self.assertNotIn("indoorTargets", document["metadata"])
            self.assertEqual(1.2, excluded[0]["localPosition"][0]["east_m"])
            self.assertEqual("室内 XYZ 或无效 WGS84 坐标不能写入 EPSG:4326 几何", excluded[0]["reason"])

    def test_static_position_and_timestamp_come_from_same_latest_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV1" / "subject1-static"
            mission.mkdir(parents=True)
            for name, seconds, longitude in (("newer", 1_700_000_020, 113.702), ("older", 1_700_000_010, 113.701)):
                metadata = {
                    "mission_id": "subject1-static", "target_id": "fixed-1", "target_type": "工事",
                    "is_moving": False, "target_latitude": 33.86, "target_longitude": longitude,
                    "localization_time": {"secs": seconds, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            document = json.loads(update_subject1_submission(root, "subject1-static").read_text(encoding="utf-8"))
            feature = document["features"][0]
            self.assertEqual([113.702, 33.86], feature["geometry"]["coordinates"])
            self.assertEqual("2023-11-15T06:13:40.000+08:00", feature["properties"]["timestamp"])

    def test_repeated_static_id_uses_last_received_report_even_with_older_source_stamp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-static-overwrite"
            mission = root / "UAV2" / mission_id
            mission.mkdir(parents=True)
            for name, source_seconds, received_ns, longitude, confidence, model in (
                ("first", 1_700_000_020, 1_800_000_000_000_000_001, 113.701, .93, "车辆1"),
                ("second", 1_700_000_010, 1_800_000_000_000_000_002, 113.702, .21, "车辆2"),
            ):
                metadata = dict(mission_id=mission_id, target_id="same-static-id",
                                target_type="车辆", target_model=model, is_moving=False,
                                target_latitude=33.86, target_longitude=longitude,
                                confidence=confidence, detection_received_at_unix_ns=received_ns,
                                localization_time=dict(secs=source_seconds, nsecs=0))
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(name.encode())
            result = update_subject1_submission(root, mission_id, publisher_dedup=True)
            feature, = json.loads(result.read_text(encoding="utf-8"))["features"]
            self.assertEqual("target-001", feature["id"])
            self.assertEqual([113.702, 33.86], feature["geometry"]["coordinates"])
            self.assertEqual("2023-11-15T06:13:30.000+08:00", feature["properties"]["timestamp"])
            self.assertEqual("车辆2", feature["properties"]["targetModel"])
            self.assertEqual(.21, feature["properties"]["confidence"])
            self.assertEqual(b"second", (result.parent / feature["properties"]["imagePath"]).read_bytes())

    def test_json_only_last_static_report_does_not_reuse_old_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission_id = "subject1-static-json-only"
            mission = root / "UAV1" / mission_id
            mission.mkdir(parents=True)
            for index in (1, 2):
                metadata = dict(mission_id=mission_id, target_id="same-static-id",
                                target_type="工事", is_moving=False,
                                target_latitude=33.86, target_longitude=113.7 + index / 1000,
                                confidence=index / 10,
                                detection_received_at_unix_ns=1_800_000_000_000_000_000 + index,
                                localization_time=dict(secs=1_700_000_000 + index, nsecs=0))
                (mission / ("report-%d.json" % index)).write_text(json.dumps(metadata), encoding="utf-8")
            (mission / "report-1.jpg").write_bytes(b"old image")
            result = update_subject1_submission(root, mission_id)
            feature, = json.loads(result.read_text(encoding="utf-8"))["features"]
            self.assertEqual([113.702, 33.86], feature["geometry"]["coordinates"])
            self.assertNotIn("imagePath", feature["properties"])

    def test_concurrent_rebuilds_leave_complete_json_and_no_partial_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV3" / "subject1-concurrent"
            mission.mkdir(parents=True)
            for index in range(12):
                metadata = {
                    "mission_id": "subject1-concurrent", "target_id": "target-%d" % index,
                    "target_type": "车辆", "target_latitude": 33.86,
                    "target_longitude": 113.70 + index / 10000,
                    "localization_time": {"secs": 1_700_000_000 + index, "nsecs": 0},
                }
                (mission / ("image-%d.json" % index)).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / ("image-%d.jpg" % index)).write_bytes(b"jpeg-%d" % index)
            barrier = threading.Barrier(4)

            def rebuild():
                barrier.wait(timeout=5)
                return update_subject1_submission(root, "subject1-concurrent")

            with ThreadPoolExecutor(max_workers=4) as executor:
                targets = list(executor.map(lambda _: rebuild(), range(4)))
            self.assertEqual(1, len(set(targets)))
            document = json.loads(targets[0].read_text(encoding="utf-8"))
            self.assertEqual(12, len(document["features"]))
            image_dir = targets[0].parent / "images"
            self.assertEqual(12, len(list(image_dir.glob("*.jpg"))))
            self.assertEqual([], list(image_dir.glob("*.part")))

    def test_first_static_category_is_locked_while_last_report_corrects_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mission = root / "UAV2" / "subject1-correction"
            mission.mkdir(parents=True)
            reports = (
                ("a", 1_700_000_010, False, "人员", "人员1", 113.700),
                ("z", 1_700_000_020, True, "车辆", "车辆2", 113.701),
            )
            for name, seconds, moving, target_type, model, longitude in reports:
                metadata = {
                    "mission_id": "subject1-correction", "global_id": "one-global-id",
                    "target_type": target_type, "target_model": model, "is_moving": moving,
                    "target_latitude": 33.86, "target_longitude": longitude,
                    "localization_time": {"secs": seconds, "nsecs": 0},
                }
                (mission / (name + ".json")).write_text(json.dumps(metadata), encoding="utf-8")
                (mission / (name + ".jpg")).write_bytes(b"jpeg")
            document = json.loads(update_subject1_submission(root, "subject1-correction").read_text(encoding="utf-8"))
            self.assertEqual(1, len(document["features"]))
            feature = document["features"][0]
            self.assertEqual("target-001", feature["id"])
            self.assertEqual("Point", feature["geometry"]["type"])
            self.assertEqual("固定", feature["properties"]["targetCategory"])
            self.assertEqual("车辆", feature["properties"]["targetType"])
            self.assertEqual("车辆2", feature["properties"]["targetModel"])
            self.assertEqual([113.701, 33.86], feature["geometry"]["coordinates"])
            self.assertNotIn("trackPoints", feature["properties"])

    def test_same_file_name_from_two_uavs_keeps_both_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for uav_id in (1, 2):
                mission = root / ("UAV%d" % uav_id) / "subject1-peers"
                mission.mkdir(parents=True)
                metadata = {
                    "mission_id": "subject1-peers", "global_id": "same-target",
                    "target_type": "车辆", "is_moving": True,
                    "target_latitude": 33.86, "target_longitude": 113.70 + uav_id / 1000,
                    "localization_time": {"secs": 1_700_000_000 + uav_id, "nsecs": 0},
                }
                (mission / "capture.json").write_text(json.dumps(metadata), encoding="utf-8")
                (mission / "capture.jpg").write_bytes(("jpeg-%d" % uav_id).encode())
            target = update_subject1_submission(root, "subject1-peers")
            images = list((target.parent / "images").glob("*.jpg"))
            self.assertEqual(2, len(images))
            self.assertEqual({b"jpeg-1", b"jpeg-2"}, {path.read_bytes() for path in images})
            document = json.loads(target.read_text(encoding="utf-8"))
            # 同 ID 不代表跨机同一轨迹；各机仅一点时不可拼成虚假的动目标。
            self.assertEqual([], document["features"])
            audit = json.loads(target.with_name("submission-format.json").read_text(encoding="utf-8"))
            self.assertEqual(2, len(audit["excluded_targets"]))

    def test_slow_submission_rebuild_does_not_block_new_image_save(self):
        package_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "receiver_for_submission_test", package_root / "ground" / "ground_image_receiver.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            receiver = module.GroundImageReceiver("127.0.0.1", 0, Path(directory), "", status_interval=0)
            first_build_started = threading.Event()
            release_build = threading.Event()

            def slow_rebuild(*_args):
                first_build_started.set()
                release_build.wait(timeout=5)
                result = Path(directory) / "finished.json"
                result.write_text('{"features":[],"metadata":{"indoorTargets":[]}}', encoding="utf-8")
                return result

            metadata = {"uav_id": 1, "mission_id": "subject1-async", "request_id": "first"}
            with patch.object(module, "update_subject1_submission", side_effect=slow_rebuild):
                try:
                    receiver._save_image(metadata, b"jpeg-1")
                    self.assertTrue(first_build_started.wait(timeout=2))
                    second_saved = threading.Event()

                    def save_second():
                        receiver._save_image(dict(metadata, request_id="second"), b"jpeg-2")
                        second_saved.set()

                    worker = threading.Thread(target=save_second)
                    worker.start()
                    self.assertTrue(second_saved.wait(timeout=1), "图片保存被结果整理阻塞")
                    worker.join(timeout=1)
                    self.assertTrue((Path(directory) / "UAV1" / "subject1-async" / "second.json").exists())
                finally:
                    release_build.set()
                    receiver.stop()

    def test_failed_background_rebuild_retries_without_another_image(self):
        package_root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "receiver_for_retry_test", package_root / "ground" / "ground_image_receiver.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            receiver = module.GroundImageReceiver("127.0.0.1", 0, Path(directory), "", status_interval=0)
            attempts = []
            recovered = threading.Event()

            def flaky_rebuild(*_args):
                attempts.append(1)
                if len(attempts) == 1:
                    raise OSError("临时文件被占用")
                result = Path(directory) / "finished.json"
                result.write_text('{"features":[],"metadata":{"indoorTargets":[]}}', encoding="utf-8")
                recovered.set()
                return result

            with patch.object(module, "update_subject1_submission", side_effect=flaky_rebuild):
                try:
                    receiver._save_image(
                        {"uav_id": 1, "mission_id": "subject1-retry", "request_id": "only"}, b"jpeg"
                    )
                    self.assertTrue(recovered.wait(timeout=4), "无后续图片时应自动重试整理")
                    self.assertEqual(2, len(attempts))
                finally:
                    receiver.stop()


if __name__ == "__main__":
    unittest.main()
