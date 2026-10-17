"""One current fixed-target file; update only after the publisher's flight ends."""

import json
import os
from pathlib import Path
import tempfile
import threading

from competition_shared.fixed_targets import (
    empty_fixed_targets, fixed_targets_from_submission, validate_fixed_targets,
)
from competition_shared.submission_format import submission_content_hash


class FixedTargetStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def read(self):
        with self._lock:
            if not self.path.exists():
                return empty_fixed_targets()
            return validate_fixed_targets(json.loads(self.path.read_text(encoding="utf-8-sig")))

    def replace(self, document):
        document = validate_fixed_targets(document)
        with self._lock:
            try:
                if self.path.is_file() and self.read() == document:
                    return False
            except (OSError, ValueError):
                pass  # An authoritative update can repair an invalid local copy.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return True


class FixedTargetRecorder:
    def __init__(self, store, image_root, completed_flight, is_publisher, audit=None):
        self.store = store
        self.image_root = Path(image_root)
        self.completed_flight = completed_flight
        self.is_publisher = is_publisher
        self.audit = audit
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._last_error = ""
        self._processed_mission = None
        self._last_document = None
        self._manually_edited_mission = None

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fixed-target-recording", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self):
        while not self._stop.wait(1):
            self.tick()

    def tick(self):
        with self._lock:
            try:
                if not self.is_publisher():
                    return False
                flight = self.completed_flight()
                if not flight or flight.get("subject") != "subject1":
                    return False
                mission_id = flight["mission_id"]
                if self._manually_edited_mission == mission_id:
                    return False
                before = self.store.read()
                if self._processed_mission == mission_id and before != self._last_document:
                    # 赛后人工修正优先；同一轮迟到成果也不能把人工修正覆盖回去。
                    self._manually_edited_mission = mission_id
                    if self.audit:
                        self.audit.record("保留赛后人工修改的目标记录", mission_id=mission_id)
                    return False
                # Validate the identifier before using it in a path.
                validate_fixed_targets(dict(empty_fixed_targets(), source_mission_id=mission_id))
                root = self.image_root / "subject1_submissions" / mission_id
                path = root / "target-submission.json"
                if not path.is_file():
                    return False  # Do not erase the previous flight while results are still arriving.
                document = json.loads(path.read_text(encoding="utf-8"))
                report = json.loads((root / "submission-format.json").read_text(encoding="utf-8"))
                if not isinstance(report.get("deduplication"), dict):
                    # Standalone publishers may not have enabled the continuous publisher collector.
                    from .subject1_reporting import Subject1Reporter
                    document = Subject1Reporter(self.image_root, publisher_dedup=True).build(mission_id, document["name"])
                    report = json.loads((root / "submission-format.json").read_text(encoding="utf-8"))
                if report.get("document_sha256") != submission_content_hash(document):
                    return False  # The two atomic result files may be between generations; retry next tick.
                current = fixed_targets_from_submission(document, mission_id)
                if not self.is_publisher() or self.completed_flight() != flight or self._stop.is_set():
                    return False
                if self.store.read() != before:
                    self._manually_edited_mission = mission_id
                    if self.audit:
                        self.audit.record("保留成果整理期间人工修改的目标记录", mission_id=mission_id)
                    return False
                changed = self.store.replace(current)
                self._processed_mission = mission_id
                self._last_document = current
                self._last_error = ""
                if changed and self.audit:
                    self.audit.record("飞行结束后定点目标已更新", mission_id=mission_id,
                                      target_count=len(current["targets"]), file=str(self.store.path))
                return changed
            except (OSError, ValueError, KeyError, TypeError) as error:
                if self.audit and str(error) != self._last_error:
                    self.audit.record("定点目标更新待重试", error=str(error))
                self._last_error = str(error)
                return False
