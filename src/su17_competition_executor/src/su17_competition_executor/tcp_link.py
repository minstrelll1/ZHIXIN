from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class OnboardTcpLink:
    """Persistent outbound TCP link from one UAV to the Windows backend."""

    def __init__(
        self,
        uav_id: int,
        ground_host: str,
        ground_port: int,
        auth_token: str,
        command_handler: Callable[[Dict[str, Any]], None],
        reconnect_seconds: float = 1.0,
    ) -> None:
        self.uav_id = int(uav_id)
        self.ground_host = ground_host
        self.ground_port = int(ground_port)
        self.auth_token = auth_token
        self.command_handler = command_handler
        self.reconnect_seconds = max(0.2, float(reconnect_seconds))
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._reliable_queue: Deque[Dict[str, Any]] = deque(maxlen=1000)
        self._latest_telemetry: Optional[Dict[str, Any]] = None
        self._socket: Optional[socket.socket] = None

    @property
    def connected(self) -> bool:
        return self._connected_event.is_set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="onboard-ground-tcp", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            connection = self._socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def send_reliable(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._reliable_queue.append(dict(payload))

    def update_telemetry(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._latest_telemetry = dict(payload)

    @staticmethod
    def _encode(payload: Dict[str, Any]) -> bytes:
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise ValueError("TCP message exceeds the size limit")
        return encoded

    def _hello(self) -> Dict[str, Any]:
        return {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "uav_id": self.uav_id,
            "auth_token": self.auth_token,
        }

    def _next_outbound(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._reliable_queue:
                return self._reliable_queue.popleft()
            telemetry = self._latest_telemetry
            self._latest_telemetry = None
            return telemetry

    def _restore_reliable(self, payload: Dict[str, Any]) -> None:
        if payload.get("type") == "telemetry":
            with self._lock:
                self._latest_telemetry = payload
            return
        with self._lock:
            self._reliable_queue.appendleft(payload)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                connection = socket.create_connection(
                    (self.ground_host, self.ground_port), timeout=3.0
                )
                connection.settimeout(None)
                with self._lock:
                    self._socket = connection
                connection.sendall(self._encode(self._hello()))
                self._serve_connection(connection)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            finally:
                self._connected_event.clear()
                with self._lock:
                    connection = self._socket
                    self._socket = None
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
            self._stop_event.wait(self.reconnect_seconds)

    def _serve_connection(self, connection: socket.socket) -> None:
        buffer = bytearray()
        hello_confirmed = False
        hello_deadline = time.time() + 5.0
        while not self._stop_event.is_set():
            readable, _, exceptional = select.select(
                [connection], [], [connection], 0.1
            )
            if exceptional:
                raise OSError("TCP connection exception")
            if readable:
                chunk = connection.recv(65536)
                if not chunk:
                    raise OSError("ground TCP connection closed")
                buffer.extend(chunk)
                if len(buffer) > MAX_MESSAGE_BYTES:
                    raise ValueError("incoming TCP message exceeds the size limit")
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    message = json.loads(raw.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("TCP JSON root must be an object")
                    if not hello_confirmed:
                        if (
                            message.get("type") != "hello_ack"
                            or int(message.get("uav_id", -1)) != self.uav_id
                            or int(message.get("protocol_version", -1))
                            != PROTOCOL_VERSION
                        ):
                            raise ValueError("invalid ground hello acknowledgement")
                        hello_confirmed = True
                        self._connected_event.set()
                    else:
                        self.command_handler(message)
            if not hello_confirmed:
                if time.time() >= hello_deadline:
                    raise OSError("ground hello acknowledgement timed out")
                continue
            outbound = self._next_outbound()
            if outbound is not None:
                try:
                    connection.sendall(self._encode(outbound))
                except OSError:
                    self._restore_reliable(outbound)
                    raise
