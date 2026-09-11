from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional


class EventJournal:
    def __init__(self, directory: Optional[str]) -> None:
        self._lock = threading.Lock()
        self._directory = Path(directory).resolve() if directory else None
        self._last_snapshot_content: Optional[str] = None
        if self._directory:
            self._directory.mkdir(parents=True, exist_ok=True)

    def append(self, event: Dict[str, Any]) -> None:
        if not self._directory:
            return
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with (self._directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def save_snapshot(self, snapshot: Dict[str, Any]) -> None:
        if not self._directory:
            return
        target = self._directory / "mission_snapshot.json"
        temporary = self._directory / "mission_snapshot.json.tmp"
        content = json.dumps(snapshot, ensure_ascii=False, indent=2)
        with self._lock:
            if content == self._last_snapshot_content:
                return
            temporary.write_text(content, encoding="utf-8")
            os.replace(str(temporary), str(target))
            self._last_snapshot_content = content
