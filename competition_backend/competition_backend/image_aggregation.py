from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=8192)
def _unchanged_file_sha256(path: str, size: int, mtime_ns: int) -> str:
    # 每秒核对清单时不再重复读取所有历史图片。接收器原子替换文件会改变 mtime_ns。
    return sha256_file(Path(path))


def local_image_manifest(root: Path, uav_id: int) -> List[Dict[str, Any]]:
    """List completed image artifacts below exactly one UAV directory."""
    base = (root / "UAV{}".format(int(uav_id))).resolve()
    if not base.exists():
        return []
    result = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".json"):
            continue
        resolved = path.resolve()
        if base not in resolved.parents:
            continue
        stat = resolved.stat()
        result.append(
            {
                "relative_path": resolved.relative_to(root.resolve()).as_posix(),
                "size": stat.st_size,
                "sha256": _unchanged_file_sha256(str(resolved), stat.st_size, stat.st_mtime_ns),
            }
        )
    return result


def resolve_image_file(root: Path, uav_id: int, relative_path: str) -> Path:
    root = root.resolve()
    base = (root / "UAV{}".format(int(uav_id))).resolve()
    candidate = (root / Path(relative_path)).resolve()
    if base not in candidate.parents or not candidate.is_file():
        raise ValueError("image path is outside this computer's UAV directory")
    if candidate.suffix.lower() not in (".jpg", ".jpeg", ".json"):
        raise ValueError("unsupported image artifact")
    return candidate


class PeerImageCollector:
    """发布端持续汇集各机结果；每台对端独立核对，避免慢终端阻塞其他无人机。"""

    def __init__(
        self,
        root: Path,
        local_uav_id: int,
        peers: Dict[int, str],
        peer_token: str,
        interval_sec: float = 1.0,
        should_collect: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.root = root.resolve()
        self.local_uav_id = int(local_uav_id)
        self.peers = dict(peers)
        self.peer_token = peer_token
        self.interval_sec = max(1.0, float(interval_sec))
        self.should_collect = should_collect or (lambda: True)
        self._stop_event = threading.Event()
        self._threads: Dict[int, threading.Thread] = {}
        self._aggregate_thread: Optional[threading.Thread] = None
        self._pending_event = threading.Event()
        self._state_lock = threading.RLock()
        self._pending_missions: Set[str] = set()
        self.last_errors: Dict[int, str] = {}
        self.downloaded_count = 0
        self.aggregated_count = 0
        self.last_aggregated_at: Optional[float] = None
        self.last_aggregation_error = ""
        self._aggregation_errors: Dict[str, str] = {}
        self.peer_status: Dict[int, Dict[str, Any]] = {
            uid: {"last_success_at": None, "last_download_at": None, "downloaded_count": 0}
            for uid in self.peers if uid != self.local_uav_id
        }

    def start(self) -> None:
        if self._aggregate_thread is not None and self._aggregate_thread.is_alive():
            if not self._stop_event.is_set():
                return
            self._aggregate_thread.join(timeout=2.0)
            if self._aggregate_thread.is_alive():
                raise RuntimeError("上次图片汇总线程尚未结束，请稍后重试")
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._pending_event.clear()
        self._aggregate_thread = threading.Thread(
            target=self._aggregate_loop, name="peer-image-aggregation", daemon=True
        )
        self._aggregate_thread.start()
        for uav_id, base_url in sorted(self.peers.items()):
            if uav_id == self.local_uav_id:
                continue
            thread = threading.Thread(
                target=self._peer_loop,
                args=(uav_id, base_url),
                name="peer-image-uav{}".format(uav_id),
                daemon=True,
            )
            self._threads[uav_id] = thread
            thread.start()
        # 发布端重启后也要补齐上次已经同步到本机的结果，不依赖再次规划。
        for metadata_path in self.root.glob("UAV*/*/*.json"):
            self._queue_mission(metadata_path.parent.name)

    def stop(self) -> None:
        self._stop_event.set()
        self._pending_event.set()
        deadline = time.monotonic() + 2.0
        for thread in list(self._threads.values()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._aggregate_thread is not None:
            self._aggregate_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads.clear()

    def activate(self) -> None:
        # 兼容原调用方；同步资格现在只由 should_collect()（任务发布端角色）决定。
        self._pending_event.set()

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            details = {
                "downloaded_count": self.downloaded_count,
                "aggregated_count": self.aggregated_count,
                "last_aggregated_at": self.last_aggregated_at,
                "last_aggregation_error": self.last_aggregation_error,
                "pending_missions": sorted(self._pending_missions),
                "last_errors": dict(self.last_errors),
                "peers": {str(uid): dict(value) for uid, value in self.peer_status.items()},
            }
        try:
            active = bool(self._aggregate_thread and self._aggregate_thread.is_alive()) and not self._stop_event.is_set() and bool(self.should_collect())
        except Exception:
            active = False
        return {
            "active": active,
            **details,
        }

    def _request(self, url: str) -> urllib.request.Request:
        return urllib.request.Request(
            url, headers={"X-Competition-Peer-Token": self.peer_token}
        )

    def _sync_peer(self, uav_id: int, base_url: str) -> None:
        manifest_url = "{}/api/v1/peer/images/manifest?uav_id={}".format(
            base_url.rstrip("/"), uav_id
        )
        with urllib.request.urlopen(self._request(manifest_url), timeout=4.0) as response:
            manifest = json.loads(response.read().decode("utf-8"))["files"]
        for item in manifest:
            relative = str(item["relative_path"])
            expected_size = int(item["size"])
            expected_sha = str(item["sha256"])
            target = (self.root / Path(relative)).resolve()
            expected_root = self.root / "UAV{}".format(uav_id)
            if expected_root not in target.parents:
                raise ValueError("peer returned an unsafe image path")
            if target.is_file():
                stat = target.stat()
                if (stat.st_size == expected_size and
                        _unchanged_file_sha256(str(target), stat.st_size, stat.st_mtime_ns) == expected_sha):
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            file_url = "{}/api/v1/peer/images/file?{}".format(
                base_url.rstrip("/"),
                urllib.parse.urlencode({"uav_id": uav_id, "relative_path": relative}),
            )
            temp = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
            try:
                with urllib.request.urlopen(self._request(file_url), timeout=15.0) as response:
                    with temp.open("wb") as stream:
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                if temp.stat().st_size != expected_size or sha256_file(temp) != expected_sha:
                    raise OSError("downloaded image verification failed")
                os.replace(str(temp), str(target))
            finally:
                temp.unlink(missing_ok=True)
            with self._state_lock:
                self.downloaded_count += 1
                self.peer_status[uav_id]["downloaded_count"] += 1
                self.peer_status[uav_id]["last_download_at"] = time.time()
            if target.suffix.lower() in (".jpg", ".jpeg", ".json"):
                self._queue_mission(target.parent.name)
            if getattr(self, "audit", None):
                self.audit.record("跨地面端结果文件已同步", uav_id=uav_id, file=relative, bytes=expected_size, sha256=expected_sha)

    def _queue_mission(self, mission_id: str) -> None:
        if mission_id.startswith("subject1"):
            with self._state_lock:
                self._pending_missions.add(mission_id)
            self._pending_event.set()

    def _peer_loop(self, uav_id: int, base_url: str) -> None:
        while not self._stop_event.is_set():
            try:
                if self.should_collect():
                    self._sync_peer(uav_id, base_url)
                    with self._state_lock:
                        recovered = uav_id in self.last_errors
                        self.last_errors.pop(uav_id, None)
                        self.peer_status[uav_id]["last_success_at"] = time.time()
                    if recovered and getattr(self, "audit", None):
                        self.audit.record("跨地面端结果同步恢复", uav_id=uav_id)
            except Exception as error:  # 网络故障保留错误并在下一轮继续核对
                with self._state_lock:
                    changed = self.last_errors.get(uav_id) != str(error)
                    self.last_errors[uav_id] = str(error)
                if changed and getattr(self, "audit", None):
                    self.audit.record("跨地面端结果同步失败", uav_id=uav_id, error=str(error))
            self._stop_event.wait(self.interval_sec)

    def _aggregate_loop(self) -> None:
        while not self._stop_event.is_set():
            self._pending_event.wait(self.interval_sec)
            self._pending_event.clear()
            if self._stop_event.is_set():
                break
            try:
                if not self.should_collect():
                    continue
            except Exception:
                continue
            # 让同一帧的 JPG 和 JSON 先完成复制，再生成一次完整结果。
            if self._stop_event.wait(0.2):
                break
            with self._state_lock:
                missions = sorted(self._pending_missions)
                self._pending_missions.clear()
            for mission_id in missions:
                try:
                    path = self._rebuild_subject1(mission_id)
                    with self._state_lock:
                        recovered = mission_id in self._aggregation_errors
                        self._aggregation_errors.pop(mission_id, None)
                        self.aggregated_count += 1
                        self.last_aggregated_at = time.time()
                        self.last_aggregation_error = ""
                    if getattr(self, "audit", None):
                        if recovered:
                            self.audit.record("科目一跨终端结果汇总恢复", mission_id=mission_id)
                        self.audit.record("科目一跨终端结果已汇总", mission_id=mission_id, file=str(path))
                except Exception as error:
                    with self._state_lock:
                        changed = self._aggregation_errors.get(mission_id) != str(error)
                        self._aggregation_errors[mission_id] = str(error)
                        self.last_aggregation_error = str(error)
                        self._pending_missions.add(mission_id)
                    if changed and getattr(self, "audit", None):
                        self.audit.record("科目一跨终端结果汇总失败", mission_id=mission_id, error=str(error))

    def _rebuild_subject1(self, mission_id: str) -> Path:
        source = Path(__file__).resolve().parents[2] / "src/su17_image_transfer/src/su17_image_transfer/submission.py"
        spec = importlib.util.spec_from_file_location("subject1_aggregate", source)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载科目一结果整理模块")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.update_subject1_submission(self.root, mission_id)
