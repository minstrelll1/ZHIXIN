from __future__ import annotations

import hmac
import json
import logging
import math
import select
import socket
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Dict, Iterable, Optional, Tuple

from .adapter import FleetAdapter
from .models import Telemetry
from .pengfei_telemetry import sanitize_pengfei


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
COMMAND_SEND_TIMEOUT_SECONDS = 3.0
LINK_IDLE_TIMEOUT_SECONDS = 8.0


class _MessageReader:
    """保留分片缓冲；socket 超时后不能继续使用 makefile.readline。"""

    def __init__(self, connection, stop_event):
        self.connection = connection
        self.stop_event = stop_event
        self.buffer = bytearray()

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            if self.stop_event.is_set():
                raise OSError("地面服务正在停止")
            if time.monotonic() >= deadline:
                raise TimeoutError("机载链路接收超时，已释放旧连接，等待自动重连")
            ready, _, _ = select.select([self.connection], [], [], min(0.5, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            try:
                chunk = self.connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                return None
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_MESSAGE_BYTES and b"\n" not in self.buffer:
                raise ValueError("TCP 消息超出长度限制")
        raw, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        if len(raw) + 1 > MAX_MESSAGE_BYTES:
            raise ValueError("TCP 消息超出长度限制")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("TCP JSON 必须是对象")
        return payload


class _ClientConnection:
    def __init__(self, connection: socket.socket, address: Tuple[str, int]) -> None:
        self.connection = connection
        self.address = address
        self.send_lock = threading.Lock()

    def send(self, payload: Dict[str, Any], *, probe: bool = False) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise RuntimeError("TCP message exceeds the size limit")
        # 接收线程持有同一个 TCP socket，不能临时更改其超时/阻塞模式。
        # sendall 在半开连接上可能无限等待，过去会把整个规划请求锁住。
        if not self.send_lock.acquire(timeout=0.1 if probe else COMMAND_SEND_TIMEOUT_SECONDS):
            if not probe:
                self.close()
            raise TimeoutError("机载发送队列忙，本次只读采样跳过" if probe else "机载任务发送队列超时")
        try:
            done = threading.Event()
            errors = []

            def write():
                try:
                    self.connection.sendall(encoded)
                except OSError as error:
                    errors.append(error)
                finally:
                    done.set()

            threading.Thread(target=write, name="fleet-tcp-send", daemon=True).start()
            if not done.wait(COMMAND_SEND_TIMEOUT_SECONDS):
                self.close()
                raise TimeoutError("机载 TCP 发送超过 3 秒，连接已关闭；请核对任务回执")
            if errors:
                raise errors[0]
        finally:
            self.send_lock.release()

    def close(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass


class TcpFleetAdapter(FleetAdapter):
    """Cross-platform TCP server used by the Windows ground backend."""

    def __init__(
        self,
        uav_ids: Iterable[int],
        bind_host: str = "0.0.0.0",
        port: int = 56100,
        auth_token: str = "",
        identity_validator=None,
    ) -> None:
        super().__init__()
        self.uav_ids = set(int(value) for value in uav_ids)
        self.bind_host = bind_host
        self.port = int(port)
        self.auth_token = auth_token
        self.identity_validator = identity_validator
        self.identity_errors = {}
        self._identities = {}
        self._assignment_devices = {}
        self._server: Optional[socket.socket] = None
        self._stop_event = threading.Event()
        self._accept_thread: Optional[threading.Thread] = None
        self._client_threads = []
        self._lock = threading.RLock()
        self._clients: Dict[int, _ClientConnection] = {}
        self._telemetry: Dict[int, Telemetry] = {}
        self._gps_telemetry_received: Dict[int, float] = {}
        self._pengfei_telemetry_received: Dict[int, float] = {}
        self._ego_telemetry_received: Dict[int, float] = {}
        self._latest_assignments: Dict[int, Dict[str, Any]] = {}
        self._telemetry_condition = threading.Condition()
        self._telemetry_pending = set()
        self._telemetry_thread = None
        self._clock_probes = {}

    @property
    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return int(self._server.getsockname()[1])

    @property
    def connected_uav_ids(self):
        with self._lock:
            return sorted(self._clients)

    def telemetry_snapshot(self, uav_id: int) -> Optional[Telemetry]:
        """Return a copy suitable for publishing to another ground computer."""
        with self._lock:
            telemetry = self._telemetry.get(int(uav_id))
            return self._display_snapshot_locked(telemetry) if telemetry is not None else None

    def _display_snapshot_locked(self, telemetry: Telemetry) -> Telemetry:
        # 地图定位年龄只由实际遥测更新，通信心跳不刷新定位。
        received = self._gps_telemetry_received.get(telemetry.uav_id)
        age = max(0.0, time.monotonic() - received) if received is not None else None
        pengfei_received = self._pengfei_telemetry_received.get(telemetry.uav_id)
        pengfei_age = max(0.0, time.monotonic() - pengfei_received) if pengfei_received is not None else None
        ego_received = self._ego_telemetry_received.get(telemetry.uav_id)
        ego_age = telemetry.ego_exec_state_age_seconds
        if ego_age is not None and ego_received is not None:
            ego_age += max(0.0, time.monotonic() - ego_received)
        gps = dict(telemetry.gps_position) if telemetry.gps_position is not None else None
        if gps is not None:
            gps["age_seconds"] += age or 0.0
        return replace(telemetry, gps_position=gps, gps_telemetry_age_seconds=age,
                       pengfei=dict(telemetry.pengfei), pengfei_age_seconds=pengfei_age,
                       ego_exec_state_age_seconds=ego_age)

    def forward_command(
        self, uav_id: int, command_type: str, payload: Dict[str, Any]
    ) -> None:
        """Forward an authenticated peer command to this computer's local UAV."""
        handlers = {
            "assign_task": self.command_assign_task,
            "takeoff": self.command_takeoff,
            "execute_task": self.command_task,
            "return_home": self.command_return,
            "restart_executor": lambda uid, data: self._send_command(uid, "restart_executor", data),
            "restart_takeoff": lambda uid, data: self._send_command(uid, "restart_takeoff", data),
            "restart_all_programs": lambda uid, data: self._send_command(uid, "restart_all_programs", data),
        }
        try:
            handler = handlers[command_type]
        except KeyError as error:
            raise ValueError("unsupported peer command type") from error
        handler(int(uav_id), dict(payload))

    def start(self) -> None:
        if self._server is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.bind_host, self.port))
        server.listen(max(12, len(self.uav_ids) * 2))
        server.settimeout(0.5)
        self._server = server
        self._stop_event.clear()
        self._telemetry_thread = threading.Thread(target=self._telemetry_loop, name="fleet-telemetry", daemon=True)
        self._telemetry_thread.start()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fleet-tcp-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._telemetry_condition:
            self._telemetry_condition.notify_all()
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        for thread in list(self._client_threads):
            thread.join(timeout=1.0)
        self._client_threads = []
        if self._telemetry_thread is not None:
            self._telemetry_thread.join(timeout=2.0)

    def _notify_telemetry(self, telemetry):
        if self._telemetry_thread is None:
            self.emit_telemetry(telemetry)
            return
        # 规划和界面状态处理可能持锁较久，不能堵住网络线程的心跳和回执。
        # 每机只保留一次待通知，释放锁后读取最新快照，避免积压旧位置。
        with self._telemetry_condition:
            self._telemetry_pending.add(telemetry.uav_id)
            self._telemetry_condition.notify()

    def _telemetry_loop(self):
        while not self._stop_event.is_set():
            with self._telemetry_condition:
                self._telemetry_condition.wait_for(lambda: self._telemetry_pending or self._stop_event.is_set())
                if self._stop_event.is_set():
                    return
                pending = self._telemetry_pending
                self._telemetry_pending = set()
            for uid in pending:
                try:
                    snapshot = self.telemetry_snapshot(uid)
                    if snapshot is not None:
                        self.emit_telemetry(snapshot)
                except Exception:
                    logging.getLogger(__name__).exception("UAV%s 地面遥测处理失败，机地通信继续运行", uid)

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                assert self._server is not None
                connection, address = self._server.accept()
            except socket.timeout:
                continue
            except (OSError, AssertionError):
                break
            connection.settimeout(1.0)
            thread = threading.Thread(
                target=self._client_loop,
                args=(connection, address),
                name="fleet-tcp-client-{}".format(address[0]),
                daemon=True,
            )
            self._client_threads = [item for item in self._client_threads if item.is_alive()]
            self._client_threads.append(thread)
            thread.start()

    def _client_loop(self, connection: socket.socket, address: Tuple[str, int]) -> None:
        client = _ClientConnection(connection, address)
        audit = getattr(self, "audit", None)
        if audit:
            audit.record("任务链路连接", address=address)
        uav_id: Optional[int] = None
        try:
            connection.settimeout(COMMAND_SEND_TIMEOUT_SECONDS)
            stream = _MessageReader(connection, self._stop_event)
            hello = stream.read(5.0)
            if hello is None or hello.get("type") != "hello":
                raise ValueError("first TCP message must be hello")
            if int(hello.get("protocol_version", -1)) != PROTOCOL_VERSION:
                raise ValueError("unsupported TCP protocol version")
            uav_id = int(hello.get("uav_id", -1))
            if uav_id not in self.uav_ids:
                raise ValueError("unknown UAV ID")
            supplied_token = str(hello.get("auth_token", ""))
            if self.auth_token and not hmac.compare_digest(
                supplied_token, self.auth_token
            ):
                raise ValueError("TCP authentication failed")

            identity = self.identity_validator(hello) if self.identity_validator else {}
            if audit:
                audit.record("机载身份验证通过", uav_id=uav_id, address=address, identity=identity)
            with self._lock:
                previous = self._clients.get(uav_id)
                prior = self._identities.get(uav_id, {})
                if previous is not None and identity and prior.get("device_id") != identity.get("device_id"):
                    raise ValueError("该编号已有另一架飞机在线，拒绝覆盖连接")
                self._identities[uav_id] = identity
                self.identity_errors.pop(uav_id, None)
                self._get_telemetry_locked(uav_id).identity = identity
                client.clock_probe_version = hello.get('clock_probe_version')
                self._clients[uav_id] = client
            if previous is not None and previous is not client:
                previous.close()
            client.send(
                {
                    "type": "hello_ack",
                    "uav_id": uav_id,
                    "protocol_version": PROTOCOL_VERSION,
                    "server_time": time.time(),
                    "link_heartbeat": 1,
                }
            )
            # 重连只恢复链路，绝不自动重放旧任务/起飞/返航。机载继续现有任务。
            if audit:
                audit.record("任务链路握手完成", uav_id=uav_id, address=address,
                             heartbeat=hello.get("link_heartbeat") == 1)
            while not self._stop_event.is_set():
                message = stream.read(LINK_IDLE_TIMEOUT_SECONDS)
                if message is None:
                    break
                if int(message.get("uav_id", -1)) != uav_id:
                    raise ValueError("message UAV ID changed after hello")
                if message.get("type") == "clock_probe_reply":
                    # 只读请求有独立应答，不更新飞控遥测新鲜度，不经过任务状态机。
                    with self._lock:
                        pending = self._clock_probes.get(message.get('request_id'))
                        if (pending and pending['client'] is client
                                and self._clients.get(uav_id) is client):
                            pending['result'] = dict(message)
                            pending['done'].set()
                    continue
                if message.get("type") == "link_ping":
                    with self._lock:
                        if self._clients.get(uav_id) is not client:
                            break
                    # 心跳只证明双向通信可用，不能刷新飞控/GPS/目标数据的年龄。
                    pong = {"type": "link_pong", "uav_id": uav_id, "seq": message.get("seq")}
                    provider = getattr(self, "competition_time_provider", None)
                    if provider is not None:
                        clock_state = provider()
                        if clock_state.get("running") and clock_state.get("synchronized"):
                            pong["competition_time"] = clock_state
                    client.send(pong)
                    continue
                if not self._handle_message(uav_id, message, expected_client=client):
                    break
        except (ValueError, TypeError, KeyError) as error:
            if audit:
                audit.record("任务链路校验失败", uav_id=uav_id, address=address, error=str(error))
            with self._lock:
                self.identity_errors[str(uav_id)] = str(error)
            try:
                client.send({"type": "hello_error", "message": str(error)})
            except OSError:
                pass
        except OSError as error:
            if audit:
                audit.record("任务链路网络异常", uav_id=uav_id, address=address, error=str(error))
        finally:
            if audit:
                audit.record("任务链路断开", uav_id=uav_id, address=address)
            if uav_id is not None:
                with self._lock:
                    if self._clients.get(uav_id) is client:
                        self._clients.pop(uav_id, None)
                        telemetry = self._get_telemetry_locked(uav_id)
                        telemetry.connected = False
                        telemetry.received_at = time.time()
                        telemetry.ego_exec_state = None
                        telemetry.ego_exec_state_age_seconds = None
                        self._ego_telemetry_received.pop(uav_id, None)
                        snapshot = replace(telemetry)
                    else:
                        snapshot = None
                if snapshot is not None:
                    self._notify_telemetry(snapshot)
            with self._lock:
                for pending in self._clock_probes.values():
                    if pending['client'] is client:
                        pending['done'].set()
            client.close()

    def clock_probe(self, uav_id: int, timeout: float = 1.5) -> Dict[str, Any]:
        """只读采样机载启动标识/单调时钟；旧节点不会收到未知飞行指令。"""
        request_id = uuid.uuid4().hex
        with self._lock:
            client = self._clients.get(int(uav_id))
            if client is None:
                raise RuntimeError('无人机任务链路未连接，无法采样时间')
            if getattr(client, 'clock_probe_version', None) != 1:
                raise NotImplementedError('机载端尚不支持TCP只读时间探针，请更新机载竞赛代码')
            pending = dict(client=client, done=threading.Event(), result=None)
            self._clock_probes[request_id] = pending
        try:
            client.send(dict(type='clock_probe', uav_id=int(uav_id), request_id=request_id), probe=True)
            if not pending['done'].wait(timeout):
                raise TimeoutError('机地TCP只读时间采样超时')
            result = pending['result']
            if not result:
                raise RuntimeError('时间采样期间机地链路已断开')
            if result.get('error'):
                raise RuntimeError('机载时间采样失败：' + str(result['error']))
            return dict(boot_id=result['boot_id'], remote_monotonic=result['remote_monotonic'],
                        remote_wall=result['remote_wall'], transport='task_tcp')
        finally:
            with self._lock:
                self._clock_probes.pop(request_id, None)

    def _get_telemetry_locked(self, uav_id: int) -> Telemetry:
        telemetry = self._telemetry.get(uav_id)
        if telemetry is None:
            telemetry = Telemetry(uav_id=uav_id, received_at=time.time())
            self._telemetry[uav_id] = telemetry
        return telemetry

    def _handle_message(
        self,
        uav_id: int,
        message: Dict[str, Any],
        *,
        expected_client: Optional[_ClientConnection] = None,
    ) -> bool:
        message_type = message.get("type")
        with self._lock:
            # 在同一临界区校验连接并更新快照，阻止被替换的旧连接写入状态。
            if expected_client is not None and self._clients.get(uav_id) is not expected_client:
                return False
            telemetry = self._get_telemetry_locked(uav_id)
            telemetry.received_at = time.time()
            if message_type == "telemetry":
                self._gps_telemetry_received[uav_id] = time.monotonic()
                telemetry.connected = bool(message.get("connected", False))
                telemetry.armed = bool(message.get("armed", False))
                telemetry.odom_valid = bool(message.get("odom_valid", False))
                telemetry.failsafe = bool(message.get("failsafe", False))
                telemetry.control_state = int(message.get("control_state", 0))
                telemetry.battery_percentage = float(
                    message.get("battery_percentage", 0.0)
                )
                position = [float(value) for value in message["position"]]
                velocity = [float(value) for value in message["velocity"]]
                if len(position) != 3 or len(velocity) != 3:
                    raise ValueError("telemetry position and velocity must have 3 values")
                telemetry.position = position
                telemetry.velocity = velocity
                telemetry.task_phase = str(message.get("task_phase", ""))
                ack = message.get('return_ack')
                if isinstance(ack, dict) and ack:
                    telemetry.return_ack = dict(ack)
                event = message.get("successful_return")
                telemetry.successful_return = dict(event) if isinstance(event, dict) else {}
                telemetry.gps_status = int(message.get("gps_status", 0))
                telemetry.location_source = int(message.get("location_source", -1))
                telemetry.gps_num = int(message.get("gps_num", 0))
                for field_name in ("latitude", "longitude", "altitude", "rel_alt"):
                    raw_value = message.get(field_name)
                    if raw_value is None:
                        setattr(telemetry, field_name, None)
                    else:
                        value = float(raw_value)
                        setattr(telemetry, field_name, value if math.isfinite(value) else None)
                # 高精度 GPS 只供任务地图显示，不覆盖原始遥测和飞行高度。
                telemetry.gps_position = None
                gps = message.get("gps_position")
                if isinstance(gps, dict):
                    try:
                        lat, lon, age = (float(gps[key]) for key in ("latitude", "longitude", "age_seconds"))
                        if (all(math.isfinite(v) for v in (lat, lon, age))
                                and abs(lat) <= 90 and abs(lon) <= 180 and 0 <= age <= 2):
                            altitude = gps.get("altitude")
                            altitude = float(altitude) if altitude is not None else None
                            telemetry.gps_position = {
                                "latitude": lat, "longitude": lon, "age_seconds": age,
                                "source": str(gps.get("source", "mavros_global")),
                                "altitude": altitude if altitude is None or math.isfinite(altitude) else None,
                            }
                    except (KeyError, TypeError, ValueError):
                        pass
                mission_altitude = message.get("mission_altitude_m")
                if mission_altitude is not None and not math.isfinite(float(mission_altitude)):
                    raise ValueError("任务相对高度无效")
                telemetry.mission_altitude_m = float(mission_altitude) if mission_altitude is not None else None
                if isinstance(message.get("capabilities"), dict):
                    telemetry.capabilities = dict(message["capabilities"])
                # 兼容旧版机载端；没有该字段时清除旧目标，避免重连后残留。
                telemetry.pengfei = sanitize_pengfei(message.get("pengfei"))
                if telemetry.pengfei:
                    self._pengfei_telemetry_received[uav_id] = time.monotonic()
                else:
                    self._pengfei_telemetry_received.pop(uav_id, None)
                raw_ego_state = message.get("ego_exec_state")
                raw_ego_age = message.get("ego_exec_state_age_seconds")
                try:
                    ego_age = float(raw_ego_age)
                    valid_ego = (type(raw_ego_state) is int and 0 <= raw_ego_state <= 6
                                 and math.isfinite(ego_age) and ego_age >= 0.0)
                except (TypeError, ValueError, OverflowError):
                    valid_ego = False
                if valid_ego:
                    telemetry.ego_exec_state = raw_ego_state
                    telemetry.ego_exec_state_age_seconds = ego_age
                    self._ego_telemetry_received[uav_id] = time.monotonic()
                else:
                    telemetry.ego_exec_state = None
                    telemetry.ego_exec_state_age_seconds = None
                    self._ego_telemetry_received.pop(uav_id, None)
                if "task_complete" in message:
                    telemetry.task_complete = bool(message["task_complete"])
                    telemetry.task_assignment_acked = bool(message.get("task_assignment_acked"))
                    telemetry.task_assignment_mission_id = str(message.get("task_assignment_mission_id", ""))
                    telemetry.task_assignment_checksum = str(message.get("task_assignment_checksum", ""))
            elif message_type == "task_status":
                status = message.get("status", {})
                if not isinstance(status, dict):
                    raise ValueError("task_status.status must be an object")
                state = str(status.get("state", ""))
                audit = getattr(self, "audit", None)
                if audit:
                    summary = {key: status.get(key) for key in ("state", "mission_id", "assignment_checksum", "task_assignment_acked", "error", "message", "detail")}
                    previous = getattr(self, "_diagnostic_status", {})
                    if previous.get(uav_id) != summary:
                        audit.record("收到机载任务回执", uav_id=uav_id, status=summary)
                        previous[uav_id] = summary
                        self._diagnostic_status = previous
                if state == 'return_ack' and isinstance(status.get('return_ack'), dict):
                    telemetry.return_ack = dict(status['return_ack'])
                    if audit:
                        audit.record('收到机载返航回执', uav_id=uav_id, receipt=telemetry.return_ack)
                if state == "successful_return" and isinstance(status.get("event"), dict):
                    telemetry.successful_return = dict(status["event"])
                if state in ("completed", "done"):
                    telemetry.task_complete = True
                if bool(status.get("task_assignment_acked")) or state in (
                    "task_received",
                    "task_assigned",
                    "accepted",
                ):
                    telemetry.task_assignment_acked = True
                    mission_id = str(status.get("mission_id", ""))
                    checksum = str(status.get("assignment_checksum", ""))
                    if mission_id:
                        telemetry.task_assignment_mission_id = mission_id
                    if checksum:
                        telemetry.task_assignment_checksum = checksum
            elif message_type in ("heartbeat", "hello"):
                pass
            else:
                raise ValueError("unsupported onboard TCP message")
            snapshot = self._display_snapshot_locked(telemetry)
        # 回调会获取任务状态锁；规划线程持有该锁并调用本适配器发送任务。
        # 必须先释放 TCP 锁，调用方也不能在持有 TCP 锁时进入本方法。
        self._notify_telemetry(snapshot)
        return True

    def _send_command(self, uav_id: int, command_type: str, payload: Dict[str, Any]) -> None:
        message = {"type": command_type, **payload, "uav_id": uav_id}
        with self._lock:
            client = self._clients.get(uav_id)
        if client is None:
            raise RuntimeError("UAV{} TCP link is not connected".format(uav_id))
        try:
            client.send(message)
        except OSError as error:
            client.close()
            raise RuntimeError("UAV{} TCP send failed".format(uav_id)) from error

    def command_assign_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        message = {"type": "assign_task", **payload, "uav_id": uav_id}
        with self._lock:
            if self.identity_validator:
                identity = self._identities.get(uav_id, {})
                if uav_id not in self._clients or not identity.get("identity_verified"):
                    raise RuntimeError("该无人机尚未完成设备身份核验，无法分派任务")
            self._latest_assignments[uav_id] = message
        self._send_command(uav_id, "assign_task", payload)

    def command_takeoff(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._send_command(uav_id, "takeoff", payload)

    def command_task(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._send_command(uav_id, "execute_task", payload)

    def command_return(self, uav_id: int, payload: Dict[str, Any]) -> None:
        self._send_command(uav_id, "return_home", payload)
