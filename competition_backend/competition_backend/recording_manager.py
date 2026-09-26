from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


_RUN_ID = re.compile(r"^uav([1-6])_\d{8}_\d{6}$")


def decode_recording_output(raw: bytes) -> str:
    """读取 UTF-8 新日志，并兼容旧版 Windows 中文编码日志。"""
    if isinstance(raw, str):
        return raw
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    raw = raw.removeprefix(b"\xef\xbb\xbf")
    lines = []
    for line in raw.splitlines(keepends=True):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            lines.append(line.decode("gb18030", errors="replace"))
    return "".join(lines)


def parse_ssh_hosts(raw: str) -> Dict[int, str]:
    hosts: Dict[int, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        uav_id_text, separator, address = item.partition("=")
        if not separator or not address.strip():
            raise ValueError("COMPETITION_UAV_SSH_HOSTS 必须使用 无人机编号=机载地址 格式")
        uav_id = int(uav_id_text.strip())
        if not 1 <= uav_id <= 6:
            raise ValueError("采集无人机编号必须在 1～6 之间")
        hosts[uav_id] = address.strip()
    return hosts


class FlightRecordingManager:
    """Run the verified PowerShell flight recorder without interactive passwords."""

    def __init__(
        self,
        project_root: Path,
        ssh_hosts: Dict[int, str],
        ssh_user: str = "amov",
        python_executable: str = sys.executable,
        vehicle_profiles: Optional[Dict[int, Dict[str, Any]]] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.output_root = self.project_root / "flight_records"
        self.control_root = self.output_root / ".web_control"
        self.script_path = self.project_root / "tools" / "record_su17_flight.ps1"
        self.ssh_hosts = dict(ssh_hosts)
        self.ssh_user = ssh_user
        self.python_executable = python_executable
        self.vehicle_profiles = {
            int(key): dict(value) for key, value in (vehicle_profiles or {}).items()
        }
        self._lock = threading.RLock()
        self._run: Optional[Dict[str, Any]] = None

    def _require_host(self, uav_id: int) -> str:
        try:
            return self.ssh_hosts[int(uav_id)]
        except KeyError as error:
            raise ValueError(f"UAV{uav_id} 尚未配置 SSH 地址") from error

    def _check_key_login(self, uav_id: int) -> None:
        host = self._require_host(uav_id)
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            f"{self.ssh_user}@{host}",
            "printf SSH_KEY_OK",
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"UAV{uav_id} SSH 密钥登录检查失败：{error}") from error
        if result.returncode != 0 or "SSH_KEY_OK" not in decode_recording_output(result.stdout):
            detail = decode_recording_output(result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"UAV{uav_id} 尚未配置好 SSH 免密码登录，请先按部署说明安装地面电脑公钥"
                + (f"：{detail}" if detail else "")
            )

    def start(self, uav_id: int, recording_mode: str = "core") -> Dict[str, Any]:
        uav_id = int(uav_id)
        recording_mode = str(recording_mode or "core").strip().lower()
        if recording_mode == "complex":
            recording_mode = "full"
        if recording_mode not in ("core", "full"):
            raise ValueError("数据采集模式必须是 core（核心）或 full（完整）")
        with self._lock:
            self._refresh_locked()
            if self._run and self._run["state"] in ("recording", "processing"):
                raise RuntimeError("已有采集正在运行或处理，请先等待本次采集完成")
            if not self.script_path.is_file():
                raise RuntimeError(f"找不到采集脚本：{self.script_path}")
            self._check_key_login(uav_id)

            now = time.time()
            run_id = f"uav{uav_id}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}"
            if not _RUN_ID.fullmatch(run_id):
                raise RuntimeError("生成的采集编号无效")
            self.control_root.mkdir(parents=True, exist_ok=True)
            stop_signal = self.control_root / f"{run_id}.stop"
            log_path = self.control_root / f"{run_id}.log"
            stop_signal.unlink(missing_ok=True)
            host = self._require_host(uav_id)
            profile = self.vehicle_profiles.get(uav_id, {})
            model = str(profile.get("model", "su17")).strip().lower() or "su17"
            if model not in ("p600", "su17"):
                raise RuntimeError(f"UAV{uav_id} 的机型不支持数据采集：{model}")
            local_ros_uav_id = int(
                profile.get("local_ros_uav_id", 1 if model == "su17" else uav_id)
            )
            vendor_workspace = str(profile.get("vendor_workspace", "")).strip()
            if vendor_workspace.startswith("~/"):
                vendor_workspace = f"/home/{self.ssh_user}/{vendor_workspace[2:]}"
            if not vendor_workspace:
                vendor_workspace = (
                    f"/home/{self.ssh_user}/su17_experiment"
                    if model == "su17"
                    else f"/home/{self.ssh_user}/p600_experiment"
                )
            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script_path),
                "-UavAddress",
                host,
                "-UavUser",
                self.ssh_user,
                "-UavId",
                str(uav_id),
                "-Model",
                model,
                "-LocalRosUavId",
                str(local_ros_uav_id),
                "-VendorWorkspace",
                vendor_workspace,
                "-OutputRoot",
                str(self.output_root),
                "-PythonExe",
                self.python_executable,
                "-FlightName",
                run_id,
                "-NonInteractive",
                "-StopSignalPath",
                str(stop_signal),
                "-RecordingMode",
                recording_mode,
            ]
            # 以二进制写入日志，PowerShell 和 Python 自行输出 UTF-8 字节。
            log_handle = log_path.open("wb")
            child_env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.project_root),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                log_handle.close()
                raise
            self._run = {
                "run_id": run_id,
                "uav_id": uav_id,
                "host": host,
                "model": model,
                "recording_mode": recording_mode,
                "local_ros_uav_id": local_ros_uav_id,
                "position_topic": (
                    f"/uav{local_ros_uav_id}/prometheus/odom"
                    if model == "p600"
                    else "/Odometry"
                ),
                "state": "recording",
                "started_at": now,
                "stop_requested_at": None,
                "finished_at": None,
                "process": process,
                "log_handle": log_handle,
                "log_path": log_path,
                "stop_signal": stop_signal,
                "local_dir": self.output_root / run_id,
                "remote_dir": f"/home/{self.ssh_user}/flight_records/{run_id}",
                "onboard_deleted": False,
                "return_code": None,
            }
            return self._snapshot_locked()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if not self._run or self._run["state"] != "recording":
                raise RuntimeError("当前没有正在进行的飞行数据采集")
            self._run["stop_signal"].write_text("stop\n", encoding="utf-8")
            self._run["state"] = "processing"
            self._run["stop_requested_at"] = time.time()
            return self._snapshot_locked()

    def delete_onboard_backup(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if not self._run or self._run["state"] != "completed":
                raise RuntimeError("采集、文件校验和报告生成成功后才可删除机载备份")
            if self._run["onboard_deleted"]:
                return self._snapshot_locked()
            run_id = self._run["run_id"]
            match = _RUN_ID.fullmatch(run_id)
            if not match or int(match.group(1)) != self._run["uav_id"]:
                raise RuntimeError("机载目录未通过路径检查，已拒绝删除")
            remote_dir = self._run["remote_dir"]
            expected_prefix = f"/home/{self.ssh_user}/flight_records/uav{self._run['uav_id']}_"
            shell = (
                f"target='{remote_dir}'; [ ! -e \"$target\" ] && exit 0; "
                "resolved=$(realpath -e -- \"$target\") || exit 41; "
                f"case \"$resolved\" in '{expected_prefix}'*) rm -rf -- \"$resolved\" ;; *) exit 43 ;; esac; "
                f"[ ! -e '{remote_dir}' ]"
            )
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                f"{self.ssh_user}@{self._run['host']}",
                shell,
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                detail = decode_recording_output(result.stderr or result.stdout).strip()
                raise RuntimeError(
                    "机载备份删除失败"
                    + (f"：{detail}" if detail else "")
                )
            self._run["onboard_deleted"] = True
            return self._snapshot_locked()

    def report_path(self) -> Path:
        with self._lock:
            self._refresh_locked()
            if not self._run or self._run["state"] != "completed":
                raise RuntimeError("定位报告尚未生成")
            path = self._run["local_dir"] / "mid360_position_report.html"
            if not path.is_file():
                raise RuntimeError("找不到定位报告文件")
            return path

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            return self._snapshot_locked()

    def close(self) -> None:
        with self._lock:
            self._refresh_locked()
            if self._run and self._run["state"] == "recording":
                self._run["stop_signal"].write_text("stop\n", encoding="utf-8")
                self._run["state"] = "processing"
                self._run["stop_requested_at"] = time.time()

    def _refresh_locked(self) -> None:
        if not self._run:
            return
        process = self._run["process"]
        return_code = process.poll()
        if return_code is None:
            return
        if not self._run["log_handle"].closed:
            self._run["log_handle"].close()
        if self._run["finished_at"] is None:
            self._run["finished_at"] = time.time()
            self._run["return_code"] = return_code
            report = self._run["local_dir"] / "mid360_position_report.html"
            self._run["state"] = "completed" if return_code == 0 and report.is_file() else "failed"

    def _snapshot_locked(self) -> Dict[str, Any]:
        if not self._run:
            return {"state": "idle", "configured_uav_ids": sorted(self.ssh_hosts)}
        run = self._run
        now = time.time()
        report = run["local_dir"] / "mid360_position_report.html"
        log_tail = ""
        try:
            if run["log_path"].is_file():
                log_tail = "\n".join(
                    decode_recording_output(run["log_path"].read_bytes()).splitlines()[-12:]
                )
        except OSError:
            pass
        return {
            "state": run["state"],
            "run_id": run["run_id"],
            "uav_id": run["uav_id"],
            "model": run["model"],
            "recording_mode": run["recording_mode"],
            "recording_mode_label": (
                "核心数据（快速）" if run["recording_mode"] == "core"
                else "完整数据（含 MID360 点云/IMU）"
            ),
            "local_ros_uav_id": run["local_ros_uav_id"],
            "position_topic": run["position_topic"],
            "started_at": run["started_at"],
            "elapsed_seconds": max(0.0, (run["finished_at"] or now) - run["started_at"]),
            "stop_requested_at": run["stop_requested_at"],
            "finished_at": run["finished_at"],
            "return_code": run["return_code"],
            "local_directory": str(run["local_dir"]),
            "remote_directory": run["remote_dir"],
            "report_ready": report.is_file() and run["state"] == "completed",
            "report_url": "/api/v1/recording/report" if report.is_file() else None,
            "onboard_deleted": run["onboard_deleted"],
            "log_tail": log_tail,
            "configured_uav_ids": sorted(self.ssh_hosts),
        }
