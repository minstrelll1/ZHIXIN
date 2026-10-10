from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
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


def local_image_manifest(
    root: Path,
    uav_id: int,
    max_age_seconds: Optional[float] = None,
    mission_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List completed artifacts for one UAV, optionally restricting recent files or a mission."""
    root = root.resolve()
    base = (root / "UAV{}".format(int(uav_id))).resolve()
    if max_age_seconds is not None:
        max_age_seconds = float(max_age_seconds)
        if not math.isfinite(max_age_seconds) or not 0 <= max_age_seconds <= 7 * 86400:
            raise ValueError("图片清单时间范围必须在 0 秒至 7 天之间")
        # 只与对端文件自己的时间比较，不依赖两台电脑时钟同步。
        oldest_mtime = time.time() - max_age_seconds
    else:
        oldest_mtime = None
    if mission_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", mission_id):
        raise ValueError("任务编号包含非法路径字符")
    if not base.exists():
        return []
    if mission_id is not None:
        mission_dir = (base / mission_id).resolve()
        if base not in mission_dir.parents or not mission_dir.is_dir():
            return []
        directories = [mission_dir]
    elif oldest_mtime is None:
        directories = [base]
    else:
        # 接收器以原子替换保存新文件；新文件写入会更新任务目录的 mtime。
        # 先排除旧任务目录，再计算单个文件的 SHA-256，避免每秒遍历历次比赛。
        directories = [
            path for path in sorted(base.iterdir())
            if path.is_dir() and path.stat().st_mtime >= oldest_mtime
        ]
    result = []
    for directory in directories:
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in (".jpg", ".jpeg", ".json"):
                continue
            resolved = path.resolve()
            if base not in resolved.parents:
                continue
            stat = resolved.stat()
            if oldest_mtime is not None and stat.st_mtime < oldest_mtime:
                continue
            result.append(
                {
                    "relative_path": resolved.relative_to(root).as_posix(),
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
        mission_filter: Optional[Callable[[str], bool]] = None,
        publisher_dedup: bool = False,
        session_started_monotonic: Optional[float] = None,
    ) -> None:
        self.root = root.resolve()
        self.local_uav_id = int(local_uav_id)
        self.peers = dict(peers)
        self.peer_token = peer_token
        self.interval_sec = max(1.0, float(interval_sec))
        self.should_collect = should_collect or (lambda: True)
        self.mission_filter = mission_filter or (lambda _mission_id: True)
        self.publisher_dedup = bool(publisher_dedup)
        self.session_started_monotonic = session_started_monotonic
        self._seen_peer_missions: Dict[int, Set[str]] = {}
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
        query = {"uav_id": uav_id}
        if self.session_started_monotonic is not None:
            # 用对端自己的文件年龄比较，避免六台地面电脑时钟存在偏差。
            query["max_age_seconds"] = "%.3f" % max(
                0.0, time.monotonic() - self.session_started_monotonic + 2.0)
        manifest_url = "{}/api/v1/peer/images/manifest?{}".format(
            base_url.rstrip("/"), urllib.parse.urlencode(query))
        with urllib.request.urlopen(self._request(manifest_url), timeout=4.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if self.session_started_monotonic is not None and payload.get("filter_applied") is not True:
            raise RuntimeError("对端图片清单尚未支持按本次启动时间筛选，请更新该地面终端")
        manifest = payload["files"]
        # 本次启动后第一次看到某个任务的新回传时，补齐该任务较早的图片/JSON，
        # 以保留完整轨迹；其他旧任务始终不进入本次成果目录。
        new_missions = set()
        if self.session_started_monotonic is not None:
            known = self._seen_peer_missions.setdefault(uav_id, set())
            for item in manifest:
                parts = Path(str(item.get("relative_path", ""))).parts
                if len(parts) == 3 and parts[1].startswith("subject1") \
                        and self.mission_filter(parts[1]) and parts[1] not in known:
                    new_missions.add(parts[1])
            all_items = {str(item["relative_path"]): item for item in manifest}
            for mission_id in sorted(new_missions):
                full_query = urllib.parse.urlencode({"uav_id": uav_id, "mission_id": mission_id})
                full_url = "{}/api/v1/peer/images/manifest?{}".format(base_url.rstrip("/"), full_query)
                with urllib.request.urlopen(self._request(full_url), timeout=15.0) as response:
                    full = json.loads(response.read().decode("utf-8"))
                for item in full["files"]:
                    all_items[str(item["relative_path"])] = item
            manifest = list(all_items.values())
        # 赛事结果先依赖目标 JSON；不要让大图片下载或超时挡住后续目标记录。
        manifest.sort(key=lambda item: (
            0 if Path(str(item.get("relative_path", ""))).suffix.lower() == ".json" else 1,
            str(item.get("relative_path", "")),
        ))
        for item in manifest:
            relative = str(item["relative_path"])
            parts = Path(relative).parts
            if len(parts) != 3 or not self.mission_filter(parts[1]):
                continue
            if self.session_started_monotonic is not None and not parts[1].startswith("subject1"):
                continue
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
        if new_missions:
            self._seen_peer_missions[uav_id].update(new_missions)

    def _queue_mission(self, mission_id: str) -> None:
        if mission_id.startswith("subject1") and self.mission_filter(mission_id):
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
                        decisions = {}
                        if self.publisher_dedup:
                            decisions = json.loads((path.parent / "dedup-decisions.json").read_text(encoding="utf-8"))
                        self.audit.record("科目一跨终端结果已汇总", mission_id=mission_id, file=str(path),
                                          raw_count=decisions.get("raw_count"),
                                          deduplicated_count=decisions.get("deduplicated_count"),
                                          result_count=decisions.get("result_count"),
                                          backfilled_count=decisions.get("backfilled_count", 0),
                                          merged_count=len(decisions.get("merged", [])),
                                          same_uav_merged_count=sum(bool(row.get("same_uav")) for row in decisions.get("merged", [])),
                                          reclassified_static_count=len(decisions.get("motion_reclassifications", [])),
                                          omitted_count=len(decisions.get("omitted", [])))
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
        return module.update_subject1_submission(self.root, mission_id,
                                                 publisher_dedup=self.publisher_dedup)
