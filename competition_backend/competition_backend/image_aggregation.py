from __future__ import annotations

import hashlib
import json
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_image_manifest(root: Path, uav_id: int) -> List[Dict[str, Any]]:
    """List completed image artifacts below exactly one UAV directory."""
    base = (root / "UAV{}".format(int(uav_id))).resolve()
    if not base.exists():
        return []
    result = []
    for path in base.rglob("*"):
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
                "sha256": sha256_file(resolved),
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
    """Pull missing target images from the five peer ground computers."""

    def __init__(
        self,
        root: Path,
        local_uav_id: int,
        peers: Dict[int, str],
        peer_token: str,
        interval_sec: float = 5.0,
        should_collect: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.root = root.resolve()
        self.local_uav_id = int(local_uav_id)
        self.peers = dict(peers)
        self.peer_token = peer_token
        self.interval_sec = max(1.0, float(interval_sec))
        self.should_collect = should_collect or (lambda: True)
        self._enabled = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_errors: Dict[int, str] = {}
        self.downloaded_count = 0

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="peer-image-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def activate(self) -> None:
        self._enabled.set()

    def status(self) -> Dict[str, Any]:
        return {
            "active": self._enabled.is_set(),
            "downloaded_count": self.downloaded_count,
            "last_errors": dict(self.last_errors),
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
            if self.root not in target.parents:
                raise ValueError("peer returned an unsafe image path")
            if target.is_file() and target.stat().st_size == expected_size:
                if sha256_file(target) == expected_sha:
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            file_url = "{}/api/v1/peer/images/file?{}".format(
                base_url.rstrip("/"),
                urllib.parse.urlencode({"uav_id": uav_id, "relative_path": relative}),
            )
            temp = target.with_name(target.name + ".part")
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
                temp.unlink(missing_ok=True)
                raise OSError("downloaded image verification failed")
            os.replace(str(temp), str(target))
            self.downloaded_count += 1

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            if self._enabled.is_set() and self.should_collect():
                for uav_id, base_url in sorted(self.peers.items()):
                    if uav_id == self.local_uav_id:
                        continue
                    try:
                        self._sync_peer(uav_id, base_url)
                        self.last_errors.pop(uav_id, None)
                    except Exception as error:  # retry is intentionally persistent
                        self.last_errors[uav_id] = str(error)
            self._stop_event.wait(self.interval_sec)
