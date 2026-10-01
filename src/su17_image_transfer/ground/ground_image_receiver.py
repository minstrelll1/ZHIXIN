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
from su17_image_transfer.submission import update_subject1_submission


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
        self.audit = None
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

    def _diagnostic(self, event, metadata=None, **details):
        if self.audit:
            metadata = metadata or {}
            self.audit.record(event, **{key: metadata.get(key) for key in
                              ("uav_id", "mission_id", "request_id")}, **details)

    def serve_forever(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.bind, self.port))
            server.listen(16)
            server.settimeout(1.0)
            # 绑定并监听完成后再公布就绪状态，避免并发启动时读取未绑定端口。
            self.server = server
            actual_port = server.getsockname()[1]
            print("图片接收服务正在监听 %s:%d" % (self.bind, actual_port), flush=True)
            print("图片保存目录：%s" % self.output, flush=True)
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
        print("图片发送端已连接：%s:%d" % address, flush=True)
        self._diagnostic("图片链路连接", address=address)
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
                    self._diagnostic("图片接收协议或网络异常", address=address, error=str(exc))
                    print("接收 %s:%d 的数据失败：%s" % (*address, exc), flush=True)
                    break

                message_type = metadata.get("message_type", "image")
                try:
                    metadata["uav_id"] = self._validated_uav_id(metadata.get("uav_id"))
                except ValueError as exc:
                    self._diagnostic("图片帧编号校验失败", error=str(exc))
                    print("已拒绝数据帧：%s" % exc, flush=True)
                    try:
                        if message_type in ("manifest", "mission_start"):
                            send_response(client, False, {"error": str(exc)})
                        else:
                            send_ack(client, False)
                    except OSError:
                        pass
                    continue

                if self.auth_token and metadata.get("auth_token") != self.auth_token:
                    self._diagnostic("图片认证失败", address=address)
                    print("已拒绝图片：认证令牌无效", flush=True)
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
                        self._diagnostic("图片任务计时开始", metadata)
                        send_response(
                            client,
                            True,
                            {
                                "mission_id": state["mission_id"],
                                "elapsed_sec": 0.0,
                            },
                        )
                        print(
                            "任务已开始：UAV%d，任务=%s"
                            % (state["uav_id"], state["mission_id"]),
                            flush=True,
                        )
                    except (OSError, ValueError) as exc:
                        self._diagnostic("图片任务计时失败", metadata, error=str(exc))
                        send_response(client, False, {"error": str(exc)})
                    continue

                if message_type == "manifest":
                    try:
                        result = self._compare_manifest(metadata)
                        self._diagnostic("图片补传清单核对", metadata, total=result["total"], missing_count=len(result["missing"]))
                        send_response(client, True, result)
                        print(
                            "清单核对：UAV%s，任务=%s，总数=%d，缺失=%d"
                            % (
                                metadata.get("uav_id", "?"),
                                metadata.get("mission_id", "?"),
                                result["total"],
                                len(result["missing"]),
                            ),
                            flush=True,
                        )
                    except (OSError, ValueError) as exc:
                        self._diagnostic("图片清单核对失败", metadata, error=str(exc))
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
                    self._diagnostic("图片及结果已保存", metadata, bytes=len(jpeg), image_path=str(image_path))
                    send_ack(client, True)
                    print(
                        "图片已保存：UAV%s，请求=%s，字节=%d，路径=%s"
                        % (
                            metadata.get("uav_id", "?"),
                            metadata.get("request_id", "?"),
                            len(jpeg),
                            image_path,
                        ),
                        flush=True,
                    )
                except (OSError, ValueError) as exc:
                    self._diagnostic("图片保存或确认失败", metadata, error=str(exc))
                    print("图片保存失败：%s" % exc, flush=True)
                    try:
                        send_ack(client, False)
                    except OSError:
                        pass

        print("图片发送端已断开：%s:%d" % address, flush=True)
        self._diagnostic("图片链路断开", address=address)

    def _validated_uav_id(self, value) -> int:
        try:
            uav_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("无人机编号缺失或无效") from exc
        if uav_id not in self.allowed_uav_ids:
            raise ValueError("不允许 UAV%d 接入本图片接收器" % uav_id)
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
            raise ValueError("任务编号缺失或无效")
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
                    "[任务状态] UAV%d，任务=%s，已用时=%s，图片=%d"
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
            raise ValueError("图片输出路径不安全")
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
        if str(metadata.get("mission_id", "")).startswith("subject1"):
            try:
                update_subject1_submission(self.output, metadata["mission_id"])
            except (OSError, ValueError, TypeError) as exc:
                # 原始 JPG/JSON 已安全落盘；提交包生成失败不丢弃本次回传。
                print("科目一提交文件更新失败：%s" % exc, flush=True)
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
        print("正在停止图片接收服务", flush=True)
    finally:
        receiver.stop()


if __name__ == "__main__":
    main()
