#!/usr/bin/env python3
"""Ground-side TCP server that receives and saves JPEG snapshots."""

import argparse
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Dict


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from su17_image_transfer.protocol import (
    ProtocolError,
    receive_frame,
    send_ack,
    send_response,
)


SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_component(value: object, fallback: str) -> str:
    cleaned = SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return (cleaned or fallback)[:120]


class GroundImageReceiver:
    def __init__(
        self,
        bind: str,
        port: int,
        output: Path,
        auth_token: str,
        allowed_uav_ids=None,
        status_interval: float = 5.0,
        client_timeout: float = 15.0,
    ) -> None:
        self.bind = bind
        self.port = port
        self.output = output.resolve()
        self.auth_token = auth_token
        self.allowed_uav_ids = set(allowed_uav_ids or range(1, 7))
        if not self.allowed_uav_ids:
            raise ValueError("allowed_uav_ids cannot be empty")
        self.stop_event = threading.Event()
        self.server = None
        self.status_interval = status_interval
        self.client_timeout = client_timeout
        self.mission_lock = threading.Lock()
        self.mission_states = {}
        self.output.mkdir(parents=True, exist_ok=True)

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            self.server = server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.bind, self.port))
            server.listen(16)
            server.settimeout(1.0)
            actual_port = server.getsockname()[1]
            print("Listening on %s:%d" % (self.bind, actual_port), flush=True)
            print("Saving images to %s" % self.output, flush=True)
            if self.status_interval > 0:
                threading.Thread(
                    target=self._status_reporter_loop, daemon=True
                ).start()
            while not self.stop_event.is_set():
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop_event.is_set():
                        break
                    raise
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client, address),
                    daemon=True,
                )
                thread.start()

    def _handle_client(self, client: socket.socket, address) -> None:
        print("Connected: %s:%d" % address, flush=True)
        with client:
            client.settimeout(self.client_timeout)
            while not self.stop_event.is_set():
                try:
                    metadata, jpeg = receive_frame(client)
                except socket.timeout:
                    # The timeout only wakes the thread so it can check stop_event.
                    # Sparse snapshot traffic may legitimately be idle for minutes.
                    continue
                except ConnectionError:
                    break
                except (ProtocolError, OSError) as exc:
                    print("Receive error from %s:%d: %s" % (*address, exc), flush=True)
                    break

                message_type = metadata.get("message_type", "image")
                try:
                    metadata["uav_id"] = self._validated_uav_id(metadata.get("uav_id"))
                except ValueError as exc:
                    print("Rejected frame: %s" % exc, flush=True)
                    try:
                        if message_type in ("manifest", "mission_start"):
                            send_response(client, False, {"error": str(exc)})
                        else:
                            send_ack(client, False)
                    except OSError:
                        pass
                    continue

                if self.auth_token and metadata.get("auth_token") != self.auth_token:
                    print("Rejected image with invalid auth token", flush=True)
                    try:
                        if message_type in ("manifest", "mission_start"):
                            send_response(client, False, {"error": "invalid auth token"})
                        else:
                            send_ack(client, False)
                    except OSError:
                        pass
                    continue

                if message_type == "mission_start":
                    try:
                        state = self._register_mission(metadata, reset=True)
                        send_response(
                            client,
                            True,
                            {
                                "mission_id": state["mission_id"],
                                "elapsed_sec": 0.0,
                            },
                        )
                        print(
                            "Mission started: UAV%d mission=%s"
                            % (state["uav_id"], state["mission_id"]),
                            flush=True,
                        )
                    except (OSError, ValueError) as exc:
                        send_response(client, False, {"error": str(exc)})
                    continue

                if message_type == "manifest":
                    try:
                        result = self._compare_manifest(metadata)
                        send_response(client, True, result)
                        print(
                            "Manifest UAV%s mission=%s: total=%d missing=%d"
                            % (
                                metadata.get("uav_id", "?"),
                                metadata.get("mission_id", "?"),
                                result["total"],
                                len(result["missing"]),
                            ),
                            flush=True,
                        )
                    except (OSError, ValueError) as exc:
                        send_response(client, False, {"error": str(exc)})
                    continue

                if message_type != "image":
                    try:
                        send_ack(client, False)
                    except OSError:
                        pass
                    continue

                try:
                    image_path = self._save_image(metadata, jpeg)
                    send_ack(client, True)
                    print(
                        "Saved UAV%s request=%s bytes=%d -> %s"
                        % (
                            metadata.get("uav_id", "?"),
                            metadata.get("request_id", "?"),
                            len(jpeg),
                            image_path,
                        ),
                        flush=True,
                    )
                except (OSError, ValueError) as exc:
                    print("Save failed: %s" % exc, flush=True)
                    try:
                        send_ack(client, False)
                    except OSError:
                        pass

        print("Disconnected: %s:%d" % address, flush=True)

    def _validated_uav_id(self, value) -> int:
        try:
            uav_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("uav_id is missing or invalid") from exc
        if uav_id not in self.allowed_uav_ids:
            raise ValueError("uav_id %d is not allowed" % uav_id)
        return uav_id

    @staticmethod
    def _format_duration(elapsed_sec: float) -> str:
        total = max(0, int(elapsed_sec))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return "%02d:%02d:%02d" % (hours, minutes, seconds)

    def _register_mission(self, metadata: Dict, reset: bool = False) -> Dict:
        uav_id = self._validated_uav_id(metadata.get("uav_id"))
        mission_id = safe_component(metadata.get("mission_id"), "")
        if not mission_id:
            raise ValueError("mission_id is missing or invalid")
        started_unix_ns = int(
            metadata.get("mission_started_at_unix_ns", time.time_ns())
        )
        key = (uav_id, mission_id)
        now_monotonic = time.monotonic()
        with self.mission_lock:
            state = self.mission_states.get(key)
            if (
                state is None
                or reset
                or state["mission_started_at_unix_ns"] != started_unix_ns
            ):
                # If this is learned from an image after receiver restart, retain
                # approximate elapsed wall time. A direct mission_start begins at zero.
                elapsed_before_registration = 0.0
                if not reset:
                    elapsed_before_registration = max(
                        0.0, (time.time_ns() - started_unix_ns) / 1_000_000_000
                    )
                state = {
                    "uav_id": uav_id,
                    "mission_id": mission_id,
                    "mission_started_at_unix_ns": started_unix_ns,
                    "registered_monotonic": now_monotonic
                    - elapsed_before_registration,
                    "image_count": 0,
                }
                self.mission_states[key] = state
            state["last_event_monotonic"] = now_monotonic
            return dict(state)

    def _set_mission_image_count(self, metadata: Dict, image_count: int) -> None:
        state = self._register_mission(metadata)
        key = (state["uav_id"], state["mission_id"])
        with self.mission_lock:
            if key in self.mission_states:
                self.mission_states[key]["image_count"] = image_count

    def _status_reporter_loop(self) -> None:
        while not self.stop_event.wait(self.status_interval):
            now = time.monotonic()
            with self.mission_lock:
                states = [dict(state) for state in self.mission_states.values()]
            for state in sorted(states, key=lambda item: item["uav_id"]):
                elapsed = now - state["registered_monotonic"]
                print(
                    "[MISSION] UAV%d %s elapsed=%s images=%d"
                    % (
                        state["uav_id"],
                        state["mission_id"],
                        self._format_duration(elapsed),
                        state["image_count"],
                    ),
                    flush=True,
                )

    def _target_directory(self, metadata: Dict) -> Path:
        uav_id = self._validated_uav_id(metadata.get("uav_id"))
        mission_id = safe_component(metadata.get("mission_id"), "")
        if mission_id:
            directory_name = mission_id
        else:
            timestamp_ns = int(metadata.get("requested_at_unix_ns", 0))
            directory_name = "unknown_date"
            if timestamp_ns > 0:
                import datetime

                directory_name = datetime.datetime.fromtimestamp(
                    timestamp_ns / 1_000_000_000
                ).strftime("%Y%m%d")

        target_dir = (self.output / ("UAV" + str(uav_id)) / directory_name).resolve()
        if self.output not in target_dir.parents:
            raise ValueError("unsafe output path")
        return target_dir

    def _compare_manifest(self, metadata: Dict) -> Dict:
        requested_names = metadata.get("file_names")
        if not isinstance(requested_names, list):
            raise ValueError("manifest file_names must be a list")
        if len(requested_names) > 10000:
            raise ValueError("manifest contains too many files")

        target_dir = self._target_directory(metadata)
        present = set()
        if target_dir.exists():
            present = {path.name for path in target_dir.glob("*.jpg") if path.is_file()}

        valid_names = []
        for name in requested_names:
            if not isinstance(name, str):
                continue
            safe_name = safe_component(name, "")
            if safe_name == name and safe_name.lower().endswith(".jpg"):
                valid_names.append(safe_name)
        missing = [name for name in valid_names if name not in present]
        self._set_mission_image_count(metadata, len(present))
        return {"total": len(valid_names), "present": len(valid_names) - len(missing), "missing": missing}

    def _save_image(self, metadata: Dict, jpeg: bytes) -> Path:
        request_id = safe_component(metadata.get("request_id"), "capture")
        file_name = safe_component(metadata.get("file_name"), request_id + ".jpg")
        if not file_name.lower().endswith(".jpg"):
            file_name += ".jpg"
        target_dir = self._target_directory(metadata)
        target_dir.mkdir(parents=True, exist_ok=True)

        image_path = target_dir / file_name
        metadata_path = image_path.with_suffix(".json")
        image_tmp = image_path.with_suffix(".jpg.part")
        metadata_tmp = metadata_path.with_suffix(".json.part")

        with image_tmp.open("wb") as stream:
            stream.write(jpeg)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(image_tmp, image_path)

        public_metadata = dict(metadata)
        public_metadata.pop("auth_token", None)
        with metadata_tmp.open("w", encoding="utf-8") as stream:
            json.dump(public_metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(metadata_tmp, metadata_path)
        image_count = len(list(target_dir.glob("*.jpg")))
        self._set_mission_image_count(metadata, image_count)
        return image_path

    def stop(self) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.close()


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=56010, help="TCP listen port")
    parser.add_argument(
        "--output", type=Path, default=Path("received_images"), help="save directory"
    )
    parser.add_argument(
        "--token", default="", help="optional shared token expected from senders"
    )
    parser.add_argument(
        "--uav-ids",
        default="1,2,3,4,5,6",
        help="comma-separated UAV IDs accepted by this receiver",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=5.0,
        help="seconds between mission duration lines; 0 disables them",
    )
    parser.add_argument(
        "--client-timeout",
        type=float,
        default=15.0,
        help="socket wake-up interval; idle clients remain connected",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        allowed_uav_ids = {int(value.strip()) for value in args.uav_ids.split(",") if value.strip()}
    except ValueError as exc:
        raise SystemExit("--uav-ids must contain integers separated by commas") from exc
    receiver = GroundImageReceiver(
        args.bind,
        args.port,
        args.output,
        args.token,
        allowed_uav_ids,
        args.status_interval,
        args.client_timeout,
    )
    try:
        receiver.serve_forever()
    except KeyboardInterrupt:
        print("Stopping receiver", flush=True)
    finally:
        receiver.stop()


if __name__ == "__main__":
    main()
