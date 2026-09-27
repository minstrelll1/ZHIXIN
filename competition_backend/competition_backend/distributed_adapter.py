from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional

from .adapter import FleetAdapter
from .models import Telemetry
from .tcp_adapter import TcpFleetAdapter
def parse_ground_peers(raw: str) -> Dict[int, str]:
    """Parse ``UAV_ID=http://ground-computer:port`` entries."""
    peers: Dict[int, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, separator, value = entry.partition("=")
        if not separator:
            raise ValueError("ground peer entries must be UAV_ID=http://host:port")
        uav_id = int(key.strip())
        if uav_id not in range(1, 7):
            raise ValueError("ground peer UAV ID must be between 1 and 6")
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("ground peer URL must use http:// or https://")
        peers[uav_id] = url
    return peers


class DistributedFleetAdapter(FleetAdapter):
    """Six-ground-computer adapter with exactly one directly attached UAV per node."""

    def __init__(
        self,
        all_uav_ids: Iterable[int],
        local_uav_id: int,
        node_id: str,
        peers: Dict[int, str],
        bind_host: str = "0.0.0.0",
        port: int = 56100,
        uav_auth_token: str = "",
        peer_token: str = "",
        poll_interval_sec: float = 0.5,
        identity_validator=None,
    ) -> None:
        super().__init__()
        self.uav_ids = sorted(set(int(value) for value in all_uav_ids))
        self.local_uav_id = int(local_uav_id)
        if self.local_uav_id not in self.uav_ids:
            raise ValueError("local_uav_id must be a configured UAV ID")
        self.node_id = node_id.strip() or "ground-uav{}".format(self.local_uav_id)
        self.peers = dict(peers)
        self.peer_token = peer_token
        self.identity_validator = identity_validator
        self.poll_interval_sec = max(0.2, float(poll_interval_sec))
        self.local_adapter = TcpFleetAdapter(
            [self.local_uav_id], bind_host, port, uav_auth_token, identity_validator
        )
        self.local_adapter.set_telemetry_sink(self.emit_telemetry)
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._lease_lock = threading.RLock()
        self._coordinator_id = ""
        self._coordinator_seen_at = 0.0
        self._is_coordinator = False
        self._last_lease_heartbeat = 0.0
        self._last_mission_broadcast = 0.0
        self._snapshot_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._mirrored_snapshot: Optional[Dict[str, Any]] = None
        self._peer_telemetry: Dict[int, Telemetry] = {}
        self._peer_traffic: Dict[int, Dict[str, Any]] = {}
        self._peer_sync_status: Dict[int, Dict[str, Any]] = {}
        self._telemetry_lock = threading.RLock()
        self._task_publisher = False
        self._fleet_sync_status: Dict[str, Any] = {"complete": False, "in_progress": False, "peers": {}}
        self._publisher_uav_id: Optional[int] = None

    @property
    def bind_host(self) -> str:
        return self.local_adapter.bind_host

    @property
    def bound_port(self) -> int:
        return self.local_adapter.bound_port

    @property
    def connected_uav_ids(self):
        return self.local_adapter.connected_uav_ids

    def start(self) -> None:
        self.local_adapter.start()
        if self._poll_thread and self._poll_thread.is_alive():
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="ground-peer-telemetry", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=2.0)
        self.local_adapter.stop()

    def local_telemetry_dict(self) -> Optional[Dict[str, Any]]:
        telemetry = self.local_adapter.telemetry_snapshot(self.local_uav_id)
        return asdict(telemetry) if telemetry is not None else None

    def connected_uav_ids_snapshot(self) -> List[int]:
        """Read the continuously refreshed telemetry cache; never block on peers."""
        result = []
        now = time.time()
        local = self.local_adapter.telemetry_snapshot(self.local_uav_id)
        if local is not None and local.connected and now - local.received_at <= 3.0:
            result.append(self.local_uav_id)
        with self._telemetry_lock:
            for uav_id, telemetry in self._peer_telemetry.items():
                if telemetry.connected and now - telemetry.received_at <= 3.0:
                    result.append(uav_id)
        return result

    def peer_sync_status(self) -> Dict[str, Dict[str, Any]]:
        with self._telemetry_lock:
            return {
                str(uav_id): dict(status)
                for uav_id, status in self._peer_sync_status.items()
            }

    def peer_traffic_status(self) -> Dict[str, Dict[str, Any]]:
        with self._telemetry_lock:
            return {
                str(uav_id): dict(status)
                for uav_id, status in self._peer_traffic.items()
            }

    @property
    def is_coordinator(self) -> bool:
        return self._is_coordinator

    @property
    def task_publisher(self) -> bool:
        return self._task_publisher

    @property
    def publisher_uav_id(self) -> Optional[int]:
        return self._publisher_uav_id

    def remember_publisher(self, uav_id: int) -> None:
        uav_id = int(uav_id)
        if uav_id in self.uav_ids:
            self._publisher_uav_id = uav_id

    def set_task_publisher(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if not enabled and self._is_coordinator:
            self.release_coordination()
        self._task_publisher = enabled

    def peer_operator_status(self) -> Dict[int, Dict[str, Any]]:
        """并行读取地面角色，离线终端不会逐台累加超时。"""
        def query(item):
            uav_id, base_url = item
            try:
                return uav_id, self._request_json(base_url + "/api/v1/peer/operator")
            except Exception as error:
                return uav_id, {"reachable": False, "error": str(error) or error.__class__.__name__}
        entries = [(uid, url) for uid, url in self.peers.items() if uid != self.local_uav_id]
        with ThreadPoolExecutor(max_workers=5) as pool:
            return dict(pool.map(query, entries))

    def synchronize_fleet_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Push the publisher's validated fleet configuration to all peers."""
        if not self._task_publisher:
            raise RuntimeError("只有任务发布端可以同步机队配置")
        peers: Dict[str, Dict[str, Any]] = {
            str(uid): {"ok": False, "in_progress": True}
            for uid in self.uav_ids if uid != self.local_uav_id
        }
        self._fleet_sync_status = {"complete": False, "in_progress": True, "peers": peers}
        # 同时尝试旧地址和新地址，允许发布端修正地面互联 IP/端口。
        destinations = {v["uav_id"]: "http://{}:{}".format(v["peer_host"], v["web_port"])
                        for v in config["vehicles"] if v["enabled"] and v["peer_host"]}
        destinations.update(self.peers)
        def synchronize(item):
            uav_id, base_url = item
            try:
                entry = next(v for v in config["vehicles"] if v["uav_id"] == uav_id)
                alternate = "http://{}:{}".format(entry["peer_host"], entry["web_port"]) if entry["peer_host"] else base_url
                try:
                    response = self._request_json(base_url + "/api/v1/peer/fleet-config", method="POST",
                                                  payload={"publisher_id": self.node_id, "config": config})
                except Exception:
                    if alternate == base_url:
                        raise
                    response = self._request_json(alternate + "/api/v1/peer/fleet-config", method="POST",
                                                  payload={"publisher_id": self.node_id, "config": config})
                if response.get("accepted") is not True:
                    raise RuntimeError("对端未确认接收配置")
                return str(uav_id), {
                    "ok": True,
                    "restart_required": bool(response.get("restart_required")),
                    "revision": response.get("revision", ""),
                }
            except Exception as error:
                return str(uav_id), {
                    "ok": False,
                    "error": str(error) or error.__class__.__name__,
                }
        entries = [(uid, url) for uid, url in destinations.items() if uid != self.local_uav_id]
        with ThreadPoolExecutor(max_workers=5) as pool:
            peers = dict(pool.map(synchronize, entries))
        self._fleet_sync_status = {
            "complete": all(item.get("ok") for item in peers.values()),
            "in_progress": False,
            "peers": peers,
        }
        return self.fleet_sync_status()

    def fleet_sync_status(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._fleet_sync_status))

    def accept_fleet_config(self, publisher_uav_id: int) -> None:
        """记录普通终端最近一次从发布端收到的配置。"""
        self.remember_publisher(publisher_uav_id)
        self._fleet_sync_status = {
            "complete": True,
            "in_progress": False,
            "peers": {str(self.local_uav_id): {
                "ok": True, "received_from": int(publisher_uav_id)
            }},
        }

    def require_peer_coordinator(self, coordinator_id: str) -> None:
        with self._lease_lock:
            if self._coordinator_id != coordinator_id or time.time() - self._coordinator_seen_at >= 30.0:
                raise RuntimeError("发布端控制租约不存在或已过期，请重新规划分派")

    def assert_local_control_available(self) -> None:
        with self._lease_lock:
            if self._coordinator_id and self._coordinator_id != self.node_id and time.time() - self._coordinator_seen_at < 30.0:
                raise RuntimeError("本机无人机由任务发布端控制，请先结束发布端任务再本地分派")

    def set_snapshot_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        self._snapshot_provider = provider

    def mirrored_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lease_lock:
            if self._mirrored_snapshot is None:
                return None
            return json.loads(json.dumps(self._mirrored_snapshot))

    def accept_peer_snapshot(
        self, coordinator_id: str, snapshot: Dict[str, Any]
    ) -> None:
        self.claim_peer_coordinator(coordinator_id)
        with self._lease_lock:
            self._mirrored_snapshot = json.loads(json.dumps(snapshot))

    def _request_json(
        self, url: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        data = None
        headers = {"X-Competition-Peer-Token": self.peer_token}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=2.0) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("ground peer returned invalid JSON")
        return result

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._is_coordinator and time.time() - self._last_lease_heartbeat >= 5.0:
                self._heartbeat_coordination()
            if self._is_coordinator and time.time() - self._last_mission_broadcast >= 1.0:
                self._broadcast_mission_snapshot()
            for uav_id in self.uav_ids:
                if uav_id == self.local_uav_id or self._stop_event.is_set():
                    continue
                base_url = self.peers.get(uav_id)
                if not base_url:
                    continue
                try:
                    request_started = time.monotonic()
                    result = self._request_json(
                        "{}/api/v1/peer/telemetry/{}".format(base_url, uav_id)
                    )
                    raw = result.get("telemetry")
                    traffic = result.get("traffic")
                    if isinstance(traffic, dict):
                        with self._telemetry_lock:
                            self._peer_traffic[uav_id] = dict(traffic)
                    if raw:
                        # ``received_at`` from a peer is stamped by that peer's
                        # clock.  Normalize it at the local receive boundary so
                        # freshness checks and the UI latency display are not
                        # affected by clock skew between ground computers.
                        telemetry = Telemetry(**raw)
                        if telemetry.uav_id != uav_id:
                            raise ValueError("地面节点上报的无人机编号不匹配")
                        if self.identity_validator and telemetry.connected:
                            self.identity_validator({"uav_id": uav_id, **telemetry.identity})
                        received_at = time.time()
                        # 相对定位年龄跨终端传递；完整请求耗时作为保守上界。
                        transfer_age = max(0.0, time.monotonic() - request_started)
                        gps = dict(telemetry.gps_position) if telemetry.gps_position is not None else None
                        if gps is not None:
                            gps["age_seconds"] += transfer_age
                        position_age = telemetry.gps_telemetry_age_seconds
                        if position_age is not None:
                            position_age += transfer_age
                        telemetry = replace(telemetry, received_at=received_at, gps_position=gps,
                                            gps_telemetry_age_seconds=position_age)
                        with self._telemetry_lock:
                            self._peer_telemetry[uav_id] = telemetry
                            self._peer_sync_status[uav_id] = {
                                "ok": True,
                                "last_success_at": received_at,
                                "last_error": "",
                            }
                        self.emit_telemetry(telemetry)
                    else:
                        with self._telemetry_lock:
                            self._peer_sync_status[uav_id] = {
                                "ok": False,
                                "last_success_at": self._peer_sync_status.get(
                                    uav_id, {}
                                ).get("last_success_at"),
                                "last_error": "peer ground computer has no local UAV telemetry",
                            }
                except Exception as error:
                    with self._telemetry_lock:
                        previous_status = self._peer_sync_status.get(uav_id, {})
                        self._peer_sync_status[uav_id] = {
                            "ok": False,
                            "last_success_at": previous_status.get("last_success_at"),
                            "last_error": str(error) or error.__class__.__name__,
                        }
                        previous = self._peer_telemetry.get(uav_id)
                    if previous is not None and previous.connected and time.time() - previous.received_at > 3.0:
                        stale = replace(previous, connected=False, received_at=time.time())
                        with self._telemetry_lock:
                            self._peer_telemetry[uav_id] = stale
                        self.emit_telemetry(stale)
                    continue
            self._stop_event.wait(self.poll_interval_sec)

    def claim_peer_coordinator(self, coordinator_id: str) -> None:
        coordinator_id = coordinator_id.strip()
        if not coordinator_id:
            raise ValueError("coordinator_id is required")
        now = time.time()
        with self._lease_lock:
            if (
                self._coordinator_id
                and self._coordinator_id != coordinator_id
                and now - self._coordinator_seen_at < 30.0
            ):
                raise RuntimeError(
                    "commands are currently owned by {}".format(self._coordinator_id)
                )
            self._coordinator_id = coordinator_id
            self._coordinator_seen_at = now

    def release_peer_coordinator(self, coordinator_id: str) -> None:
        with self._lease_lock:
            if self._coordinator_id == coordinator_id:
                self._coordinator_id = ""
                self._coordinator_seen_at = 0.0

    def acquire_coordination(self, uav_ids: Optional[Iterable[int]] = None) -> None:
        """Reserve only ground nodes belonging to connected UAVs."""
        if not self._task_publisher:
            raise RuntimeError("只有任务发布端可以取得跨终端控制租约")
        self.claim_peer_coordinator(self.node_id)
        selected = sorted(set(int(value) for value in (uav_ids or self.uav_ids)))
        acquired = []
        try:
            for uav_id in selected:
                if uav_id == self.local_uav_id:
                    continue
                base_url = self.peers.get(uav_id)
                if not base_url:
                    raise RuntimeError("no ground peer is configured for UAV{}".format(uav_id))
                self._request_json(
                    base_url + "/api/v1/peer/lease",
                    method="POST",
                    payload={"coordinator_id": self.node_id, "action": "claim"},
                )
                acquired.append(base_url)
        except Exception:
            for base_url in acquired:
                try:
                    self._request_json(
                        base_url + "/api/v1/peer/lease",
                        method="POST",
                        payload={"coordinator_id": self.node_id, "action": "release"},
                    )
                except Exception:
                    pass
            self.release_peer_coordinator(self.node_id)
            raise
        self._is_coordinator = True
        self._last_lease_heartbeat = time.time()

    def _heartbeat_coordination(self) -> None:
        self.claim_peer_coordinator(self.node_id)
        for uav_id, base_url in self.peers.items():
            if uav_id == self.local_uav_id:
                continue
            try:
                self._request_json(
                    base_url + "/api/v1/peer/lease",
                    method="POST",
                    payload={"coordinator_id": self.node_id, "action": "claim"},
                )
            except Exception:
                pass
        self._last_lease_heartbeat = time.time()

    def _broadcast_mission_snapshot(self) -> None:
        provider = self._snapshot_provider
        if provider is None:
            return
        snapshot = provider()
        for uav_id, base_url in self.peers.items():
            if uav_id == self.local_uav_id:
                continue
            try:
                self._request_json(
                    base_url + "/api/v1/peer/mission",
                    method="POST",
                    payload={"coordinator_id": self.node_id, "snapshot": snapshot},
                )
            except Exception:
                pass
        self._last_mission_broadcast = time.time()

    def release_coordination(self) -> None:
        if not self._is_coordinator:
            return
        self._is_coordinator = False
        self.release_peer_coordinator(self.node_id)
        provider = self._snapshot_provider
        snapshot = provider() if provider is not None else None
        thread = threading.Thread(
            target=self._release_coordination_network,
            args=(snapshot,),
            name="ground-peer-release",
            daemon=True,
        )
        thread.start()

    def _release_coordination_network(
        self, snapshot: Optional[Dict[str, Any]]
    ) -> None:
        for uav_id, base_url in self.peers.items():
            if uav_id == self.local_uav_id:
                continue
            try:
                if snapshot is not None:
                    self._request_json(
                        base_url + "/api/v1/peer/mission",
                        method="POST",
                        payload={"coordinator_id": self.node_id, "snapshot": snapshot},
                    )
                self._request_json(
                    base_url + "/api/v1/peer/lease",
                    method="POST",
                    payload={"coordinator_id": self.node_id, "action": "release"},
                )
            except Exception:
                pass

    def accept_peer_command(
        self, coordinator_id: str, uav_id: int, command_type: str, payload: Dict[str, Any]
    ) -> None:
        if int(uav_id) != self.local_uav_id:
            raise ValueError("this ground computer may only control UAV{}".format(self.local_uav_id))
        self.claim_peer_coordinator(coordinator_id)
        self.local_adapter.forward_command(uav_id, command_type, payload)

    def _command(self, uav_id: int, command_type: str, payload: Dict[str, Any]) -> None:
        uav_id = int(uav_id)
        if uav_id == self.local_uav_id:
            if command_type != "return_home":
                self.assert_local_control_available()
            self.local_adapter.forward_command(uav_id, command_type, payload)
            return
        if not self._task_publisher:
            raise RuntimeError("普通地面终端只能控制本机配对无人机")
        base_url = self.peers.get(uav_id)
        if not base_url:
            raise RuntimeError("no ground peer is configured for UAV{}".format(uav_id))
        self._request_json(
            base_url + "/api/v1/peer/command",
            method="POST",
            payload={
                "coordinator_id": self.node_id,
                "uav_id": uav_id,
                "command_type": command_type,
                "payload": payload,
            },
        )

    def command_assign_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._command(uav_id, "assign_task", payload)

    def command_takeoff(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._command(uav_id, "takeoff", payload)

    def command_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._command(uav_id, "execute_task", payload)

    def command_return(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._command(uav_id, "return_home", payload)
