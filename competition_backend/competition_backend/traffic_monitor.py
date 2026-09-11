"""Per-UAV ground-to-onboard traffic accounting on Windows.

Windows TCP extended statistics are maintained by the networking stack rather
than by a single application.  They therefore cover GroundStation, the web
backend, ROSBridge and other TCP clients that communicate with the configured
onboard computer.  Enabling the counters requires an elevated backend process.
"""

from __future__ import annotations

import ctypes
import ipaddress
import platform
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple


ERROR_ACCESS_DENIED = 5
ERROR_INSUFFICIENT_BUFFER = 122
NO_ERROR = 0
AF_INET = 2
TCP_TABLE_OWNER_PID_ALL = 5
TCP_CONNECTION_ESTATS_DATA = 1

# Known TCP endpoints used by the ground/onboard stack.  The endpoint can be
# local (ground listener) or remote (onboard service), so both sides are
# checked when a connection is rendered in the UI.
TCP_PORT_FUNCTIONS = {
    9090: "机载 ROSBridge（点云/状态）",
    55555: "Prometheus TCP 链路",
    55556: "Prometheus TCP 链路",
    8889: "Prometheus UDP 飞行状态",
    56100: "地面端任务/遥测",
    56010: "地面端图片接收",
    1234: "云台视频 RTSP",
    8554: "云台视频 RTSP",
    8891: "云台视频 WebRTC 预览",
}

TRAFFIC_CATEGORIES = {
    "gimbal_video": "云台视频",
    "image_return": "图片回传",
    "rviz_pointcloud": "RViz 点云",
    "prometheus": "Prometheus",
    "task_telemetry": "任务/遥测",
    "other": "其他机地通信",
}


def classify_traffic_category(
    local_port: int, remote_port: int, remote_host: str = ""
) -> str:
    """Classify one connection using the configured protocol endpoints."""
    ports = {int(local_port), int(remote_port)}
    if 56010 in ports:
        return "image_return"
    if ports.intersection({1234, 8554, 8891}):
        return "gimbal_video"
    if 9090 in ports:
        return "rviz_pointcloud"
    if ports.intersection({55555, 55556, 8889}):
        return "prometheus"
    if 56100 in ports:
        return "task_telemetry"
    return "other"


def parse_uav_traffic_hosts(raw: str) -> Dict[int, str]:
    """Parse ``UAV_ID=IPv4`` entries separated by semicolons."""
    hosts: Dict[int, str] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, separator, value = entry.partition("=")
        if not separator:
            raise ValueError(
                "UAV traffic hosts must use UAV_ID=IPv4 entries separated by ;"
            )
        uav_id = int(key.strip())
        if uav_id not in range(1, 7):
            raise ValueError("UAV traffic host ID must be between 1 and 6")
        try:
            address = ipaddress.IPv4Address(value.strip())
        except ipaddress.AddressValueError as error:
            raise ValueError("UAV traffic hosts must use IPv4 addresses") from error
        hosts[uav_id] = str(address)
    return hosts


@dataclass(frozen=True)
class TcpCounters:
    sent_bytes: int
    received_bytes: int


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_uint32),
        ("local_addr", ctypes.c_uint32),
        ("local_port", ctypes.c_uint32),
        ("remote_addr", ctypes.c_uint32),
        ("remote_port", ctypes.c_uint32),
        ("owning_pid", ctypes.c_uint32),
    ]


class _MibTcpRow(ctypes.Structure):
    _fields_ = _MibTcpRowOwnerPid._fields_[:5]


class _TcpEstatsDataRw(ctypes.Structure):
    _fields_ = [("enable_collection", ctypes.c_ubyte)]


class _TcpEstatsDataRod(ctypes.Structure):
    _fields_ = [
        ("data_bytes_out", ctypes.c_uint64),
        ("data_segs_out", ctypes.c_uint64),
        ("data_bytes_in", ctypes.c_uint64),
        ("data_segs_in", ctypes.c_uint64),
        ("segs_out", ctypes.c_uint64),
        ("segs_in", ctypes.c_uint64),
        ("soft_errors", ctypes.c_uint32),
        ("soft_error_reason", ctypes.c_uint32),
        ("snd_una", ctypes.c_uint32),
        ("snd_nxt", ctypes.c_uint32),
        ("snd_max", ctypes.c_uint32),
        ("thru_bytes_acked", ctypes.c_uint64),
        ("rcv_nxt", ctypes.c_uint32),
        ("thru_bytes_received", ctypes.c_uint64),
    ]


def _ipv4_from_uint32(value: int) -> str:
    return socket.inet_ntoa(struct.pack("<I", value))


def _port_from_uint32(value: int) -> int:
    """Convert a Windows MIB TCP port from network to host byte order."""
    return socket.ntohs(int(value) & 0xFFFF)


def _windows_tcp_estats(
    hosts: Iterable[str],
) -> Tuple[Dict[str, Dict[Tuple[int, int, int], TcpCounters]], str]:
    """Read TCP byte counters grouped by remote IPv4 address."""
    if platform.system().lower() != "windows":
        return {}, "Windows TCP extended statistics are unavailable on this system"
    wanted = set(hosts)
    if not wanted:
        return {}, "No onboard computer IP is configured"
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")
        size = ctypes.c_uint32(0)
        result = iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
        )
        if result not in (NO_ERROR, ERROR_INSUFFICIENT_BUFFER) or not size.value:
            return {}, "Windows TCP table is unavailable"
        buffer = ctypes.create_string_buffer(size.value)
        result = iphlpapi.GetExtendedTcpTable(
            buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0
        )
        if result != NO_ERROR:
            return {}, "Windows TCP table read failed ({})".format(result)
    except (AttributeError, OSError):
        return {}, "Windows TCP extended statistics are unavailable"

    count = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint32)).contents.value
    offset = ctypes.addressof(buffer) + ctypes.sizeof(ctypes.c_uint32)
    item_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    rows: Dict[str, Dict[Tuple[int, int, int], TcpCounters]] = {}
    access_denied = False
    for index in range(count):
        entry = ctypes.cast(
            offset + index * item_size, ctypes.POINTER(_MibTcpRowOwnerPid)
        ).contents
        remote_host = _ipv4_from_uint32(entry.remote_addr)
        if remote_host not in wanted or entry.owning_pid == 0:
            continue
        row = _MibTcpRow(
            entry.state,
            entry.local_addr,
            entry.local_port,
            entry.remote_addr,
            entry.remote_port,
        )
        rw = _TcpEstatsDataRw(1)
        enabled = iphlpapi.SetPerTcpConnectionEStats(
            ctypes.byref(row),
            TCP_CONNECTION_ESTATS_DATA,
            ctypes.byref(rw),
            0,
            ctypes.sizeof(rw),
            0,
        )
        if enabled == ERROR_ACCESS_DENIED:
            access_denied = True
            continue
        if enabled != NO_ERROR:
            continue
        rod = _TcpEstatsDataRod()
        result = iphlpapi.GetPerTcpConnectionEStats(
            ctypes.byref(row),
            TCP_CONNECTION_ESTATS_DATA,
            None,
            0,
            0,
            None,
            0,
            0,
            ctypes.byref(rod),
            0,
            ctypes.sizeof(rod),
        )
        if result != NO_ERROR:
            continue
        key = (
            _port_from_uint32(entry.local_port),
            _port_from_uint32(entry.remote_port),
            entry.owning_pid,
        )
        rows.setdefault(remote_host, {})[key] = TcpCounters(
            sent_bytes=int(rod.data_bytes_out), received_bytes=int(rod.data_bytes_in)
        )
    if access_denied:
        return {}, "Run the ground backend as Administrator to read all-process TCP byte counters"
    if not rows:
        return {}, "No active TCP connection to the configured onboard computer"
    return rows, ""


class TrafficMonitor:
    """Account for categorized ground/UAV traffic.

    Windows exposes byte counters for TCP connections. Components which use a
    local relay (for example a GroundStation ROSBridge relay) can contribute
    their payload counters through record_application_bytes, avoiding the
    loopback blind spot of the Windows TCP table.
    """

    def __init__(
        self,
        hosts: Dict[int, str],
        interval_sec: float = 1.0,
        video_hosts: Optional[Mapping[int, str]] = None,
    ) -> None:
        self.hosts = dict(hosts)
        self.video_hosts = {
            int(uav_id): str(host)
            for uav_id, host in (video_hosts or {}).items()
            if str(host).strip()
        }
        self._host_to_uav = {
            host: int(uav_id) for uav_id, host in self.hosts.items()
        }
        self._host_to_uav.update(
            {
                host: int(uav_id)
                for uav_id, host in self.video_hosts.items()
            }
        )
        self.interval_sec = max(0.5, float(interval_sec))
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = time.time()
        self._previous: Dict[Tuple[int, Tuple[int, int, int]], TcpCounters] = {}
        self._totals: Dict[int, TcpCounters] = {
            uav_id: TcpCounters(0, 0) for uav_id in self.hosts
        }
        self._rates: Dict[int, TcpCounters] = {
            uav_id: TcpCounters(0, 0) for uav_id in self.hosts
        }
        self._port_rates: Dict[int, Dict[Tuple[int, int], TcpCounters]] = {
            uav_id: {} for uav_id in self.hosts
        }
        self._category_rates: Dict[int, Dict[str, TcpCounters]] = {
            uav_id: {name: TcpCounters(0, 0) for name in TRAFFIC_CATEGORIES}
            for uav_id in self.hosts
        }
        self._application_totals: Dict[int, Dict[str, TcpCounters]] = {
            uav_id: {} for uav_id in self.hosts
        }
        self._application_previous: Dict[int, Dict[str, TcpCounters]] = {
            uav_id: {} for uav_id in self.hosts
        }
        self._application_rates: Dict[int, Dict[str, TcpCounters]] = {
            uav_id: {} for uav_id in self.hosts
        }
        self._previous_at = self._started_at
        self._source = "windows-tcp-estats"
        self._error = ""

    def record_application_bytes(
        self,
        uav_id: int,
        category: str,
        *,
        sent_bytes: int = 0,
        received_bytes: int = 0,
    ) -> None:
        """Record bytes handled by a local relay or an external component."""
        uav_id = int(uav_id)
        if uav_id not in self.hosts:
            return
        category = str(category).strip() or "other"
        if category not in TRAFFIC_CATEGORIES:
            category = "other"
        with self._lock:
            previous = self._application_totals[uav_id].get(
                category, TcpCounters(0, 0)
            )
            self._application_totals[uav_id][category] = TcpCounters(
                previous.sent_bytes + max(0, int(sent_bytes)),
                previous.received_bytes + max(0, int(received_bytes)),
            )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._sample()
        self._thread = threading.Thread(
            target=self._run, name="uav-traffic-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_sec * 2))
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_sec):
            self._sample()

    def _sample(self) -> None:
        samples, error = _windows_tcp_estats(self._host_to_uav.keys())
        host_to_uav = self._host_to_uav
        now = time.time()
        with self._lock:
            elapsed = max(now - self._previous_at, 1e-6)
            rates = {uav_id: TcpCounters(0, 0) for uav_id in self.hosts}
            port_rates: Dict[int, Dict[Tuple[int, int], TcpCounters]] = {
                uav_id: {} for uav_id in self.hosts
            }
            category_deltas: Dict[int, Dict[str, TcpCounters]] = {
                uav_id: {
                    name: TcpCounters(0, 0) for name in TRAFFIC_CATEGORIES
                }
                for uav_id in self.hosts
            }
            next_previous: Dict[Tuple[int, Tuple[int, int, int]], TcpCounters] = {}
            for host, connections in samples.items():
                uav_id = host_to_uav.get(host)
                if uav_id is None:
                    continue
                total = self._totals.get(uav_id, TcpCounters(0, 0))
                for key, current in connections.items():
                    previous = self._previous.get((uav_id, key))
                    if previous is None:
                        delta = current
                    else:
                        delta = TcpCounters(
                            max(0, current.sent_bytes - previous.sent_bytes),
                            max(0, current.received_bytes - previous.received_bytes),
                        )
                    total = TcpCounters(
                        total.sent_bytes + delta.sent_bytes,
                        total.received_bytes + delta.received_bytes,
                    )
                    prior_rate = rates[uav_id]
                    rates[uav_id] = TcpCounters(
                        prior_rate.sent_bytes + delta.sent_bytes,
                        prior_rate.received_bytes + delta.received_bytes,
                    )
                    category = classify_traffic_category(
                        int(key[0]), int(key[1]), host
                    )
                    prior_category = category_deltas[uav_id].get(
                        category, TcpCounters(0, 0)
                    )
                    category_deltas[uav_id][category] = TcpCounters(
                        prior_category.sent_bytes + delta.sent_bytes,
                        prior_category.received_bytes + delta.received_bytes,
                    )
                    port_key = (int(key[0]), int(key[1]))
                    prior_port_rate = port_rates[uav_id].get(
                        port_key, TcpCounters(0, 0)
                    )
                    port_rates[uav_id][port_key] = TcpCounters(
                        prior_port_rate.sent_bytes + delta.sent_bytes,
                        prior_port_rate.received_bytes + delta.received_bytes,
                    )
                    next_previous[(uav_id, key)] = current
                self._totals[uav_id] = total
            # Include payload counters reported by local relay processes. A
            # first observation is treated as a delta, just like TCP stats.
            application_rates: Dict[int, Dict[str, TcpCounters]] = {
                uav_id: {} for uav_id in self.hosts
            }
            for uav_id in self.hosts:
                for category, current in self._application_totals[uav_id].items():
                    previous = self._application_previous[uav_id].get(
                        category, TcpCounters(0, 0)
                    )
                    delta = TcpCounters(
                        max(0, current.sent_bytes - previous.sent_bytes),
                        max(0, current.received_bytes - previous.received_bytes),
                    )
                    application_rates[uav_id][category] = delta
                    self._application_previous[uav_id][category] = current
                    total = self._totals.get(uav_id, TcpCounters(0, 0))
                    self._totals[uav_id] = TcpCounters(
                        total.sent_bytes + delta.sent_bytes,
                        total.received_bytes + delta.received_bytes,
                    )
                    prior_rate = rates[uav_id]
                    rates[uav_id] = TcpCounters(
                        prior_rate.sent_bytes + delta.sent_bytes,
                        prior_rate.received_bytes + delta.received_bytes,
                    )
                    prior = category_deltas[uav_id].get(
                        category, TcpCounters(0, 0)
                    )
                    category_deltas[uav_id][category] = TcpCounters(
                        prior.sent_bytes + delta.sent_bytes,
                        prior.received_bytes + delta.received_bytes,
                    )
            self._rates = {
                uav_id: TcpCounters(
                    int(counter.sent_bytes / elapsed),
                    int(counter.received_bytes / elapsed),
                )
                for uav_id, counter in rates.items()
            }
            self._port_rates = {
                uav_id: {
                    port_key: TcpCounters(
                        int(counter.sent_bytes / elapsed),
                        int(counter.received_bytes / elapsed),
                    )
                    for port_key, counter in entries.items()
                }
                for uav_id, entries in port_rates.items()
            }
            self._category_rates = {
                uav_id: {
                    category: TcpCounters(
                        int(counter.sent_bytes / elapsed),
                        int(counter.received_bytes / elapsed),
                    )
                    for category, counter in entries.items()
                }
                for uav_id, entries in category_deltas.items()
            }
            self._application_rates = application_rates
            self._previous = next_previous
            self._previous_at = now
            self._error = error

    def status_for_uav(self, uav_id: int) -> Dict[str, object]:
        with self._lock:
            host = self.hosts.get(int(uav_id), "")
            total = self._totals.get(int(uav_id), TcpCounters(0, 0))
            rate = self._rates.get(int(uav_id), TcpCounters(0, 0))
            categories = []
            for category, label in TRAFFIC_CATEGORIES.items():
                counter = self._category_rates.get(int(uav_id), {}).get(
                    category, TcpCounters(0, 0)
                )
                categories.append(
                    {
                        "category": category,
                        "label": label,
                        "speed_mbps": round(
                            (counter.sent_bytes + counter.received_bytes)
                            * 8
                            / 1_000_000,
                            6,
                        ),
                        "source": (
                            "windows-tcp-estats"
                            if category != "rviz_pointcloud"
                            or not self._application_rates.get(int(uav_id), {}).get(
                                category
                            )
                            else "application-relay-counter"
                        ),
                    }
                )
            port_rows = []
            for (local_port, remote_port), port_rate in sorted(
                self._port_rates.get(int(uav_id), {}).items()
            ):
                function = (
                    TCP_PORT_FUNCTIONS.get(remote_port)
                    or TCP_PORT_FUNCTIONS.get(local_port)
                    or "未分类 TCP 服务"
                )
                display_port = (
                    remote_port
                    if remote_port in TCP_PORT_FUNCTIONS
                    else local_port
                )
                port_rows.append(
                    {
                        "port": display_port,
                        "local_port": local_port,
                        "remote_port": remote_port,
                        "function": function,
                        "speed_mbps": round(
                            (port_rate.sent_bytes + port_rate.received_bytes)
                            * 8
                            / 1_000_000,
                            6,
                        ),
                    }
                )
            return {
                "uav_id": int(uav_id),
                "host": host,
                "video_host": self.video_hosts.get(int(uav_id), ""),
                "enabled": bool(
                    host
                    and (
                        not self._error
                        or bool(self._application_totals.get(int(uav_id)))
                    )
                ),
                "source": self._source,
                "sent_bytes": total.sent_bytes,
                "received_bytes": total.received_bytes,
                "total_bytes": total.sent_bytes + total.received_bytes,
                "sent_bytes_per_sec": rate.sent_bytes,
                "received_bytes_per_sec": rate.received_bytes,
                "total_bytes_per_sec": rate.sent_bytes + rate.received_bytes,
                "total_speed_mbps": round(
                    (rate.sent_bytes + rate.received_bytes) * 8 / 1_000_000,
                    6,
                ),
                "error": self._error,
                "scope": "all categorized TCP traffic to configured UAV/camera endpoints plus reported local-relay payloads",
                "udp_accounting": "application-report-required",
                "categories": categories,
                "ports": port_rows,
            }

    def status(self) -> Dict[str, object]:
        return {
            "source": self._source,
            "video_hosts": {
                str(uav_id): host for uav_id, host in sorted(self.video_hosts.items())
            },
            "started_at": self._started_at,
            "elapsed_seconds": max(0.0, time.time() - self._started_at),
            "by_uav": {
                str(uav_id): self.status_for_uav(uav_id)
                for uav_id in sorted(self.hosts)
            },
            "scope": "per-UAV categorized ground/UAV traffic; TCP is read from Windows extended stats and relay payloads are reported by the component",
        }
