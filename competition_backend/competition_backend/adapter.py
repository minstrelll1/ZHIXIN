from __future__ import annotations

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict
from typing import Any, Callable, Dict, List, Optional

from .models import Telemetry


TelemetrySink = Callable[[Telemetry], None]


class FleetAdapter(ABC):
    def __init__(self) -> None:
        self._telemetry_sink: Optional[TelemetrySink] = None

    def set_telemetry_sink(self, sink: TelemetrySink) -> None:
        self._telemetry_sink = sink

    def emit_telemetry(self, telemetry: Telemetry) -> None:
        if self._telemetry_sink is not None:
            self._telemetry_sink(telemetry)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def release_coordination(self) -> None:
        """Release any distributed-ground command lease after a mission ends."""
        return None

    @abstractmethod
    def command_assign_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def command_takeoff(self, uav_id: int, payload: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def command_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def command_return(self, uav_id: int, payload: Dict[str, Any]) -> None:
        raise NotImplementedError


class RecordingAdapter(FleetAdapter):
    """In-memory adapter used by tests and the safe desktop simulator."""

    def __init__(self) -> None:
        super().__init__()
        self.commands: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def _record(self, uav_id: int, command_type: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self.commands.append(
                {
                    "sent_at": time.time(),
                    "uav_id": uav_id,
                    "type": command_type,
                    "payload": json.loads(json.dumps(payload)),
                }
            )

    def command_takeoff(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._record(uav_id, "takeoff", payload)

    def command_assign_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._record(uav_id, "assign_task", payload)

    def command_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._record(uav_id, "execute_task", payload)

    def command_return(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._record(uav_id, "return_home", payload)

    def inject(self, telemetry: Telemetry) -> None:
        self.emit_telemetry(telemetry)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.commands)
