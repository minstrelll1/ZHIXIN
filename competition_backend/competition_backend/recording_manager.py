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


def parse_ssh_hosts(raw: str) -> Dict[int, str]:
    hosts: Dict[int, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        uav_id_text, separator, address = item.partition("=")
        if not separator or not address.strip():
            raise ValueError("COMPETITION_UAV_SSH_HOSTS must use UAV_ID=ADDRESS")
        uav_id = int(uav_id_text.strip())
        if not 1 <= uav_id <= 6:
            raise ValueError("recording UAV ID must be in [1, 6]")
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
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.output_root = self.project_root / "flight_records"
        self.control_root = self.output_root / ".web_control"
        self.script_path = self.project_root / "tools" / "record_su17_flight.ps1"
        self.ssh_hosts = dict(ssh_hosts)
        self.ssh_user = ssh_user
        self.python_executable = python_executable
        self._lock = threading.RLock()
        self._run: Optional[Dict[str, Any]] = None

    def _require_host(self, uav_id: int) -> str:
        try:
            return self.ssh_hosts[int(uav_id)]
        except KeyError as error:
            raise ValueError(f"UAV{uav_id} has no configured SSH address") from error

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
                text=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"UAV{uav_id} SSH key check failed: {error}") from error
        if result.returncode != 0 or "SSH_KEY_OK" not in result.stdout:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                f"UAV{uav_id} SSH key login is not ready"
                + (f": {detail}" if detail else "")
            )

    def start(self, uav_id: int) -> Dict[str, Any]:
        uav_id = int(uav_id)
        with self._lock:
            self._refresh_locked()
            if self._run and self._run["state"] in ("recording", "processing"):
                raise RuntimeError("a flight recording is already active")
            if not self.script_path.is_file():
                raise RuntimeError(f"recording script is missing: {self.script_path}")
            self._check_key_login(uav_id)

            now = time.time()
            run_id = f"uav{uav_id}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}"
            if not _RUN_ID.fullmatch(run_id):
                raise RuntimeError("generated an invalid recording ID")
            self.control_root.mkdir(parents=True, exist_ok=True)
            stop_signal = self.control_root / f"{run_id}.stop"
            log_path = self.control_root / f"{run_id}.log"
            stop_signal.unlink(missing_ok=True)
            log_handle = log_path.open("w", encoding="utf-8")
            host = self._require_host(uav_id)
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
                "-OutputRoot",
                str(self.output_root),
                "-PythonExe",
                self.python_executable,
                "-FlightName",
                run_id,
                "-NonInteractive",
                "-StopSignalPath",
                str(stop_signal),
            ]
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.project_root),
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
                raise RuntimeError("no flight recording is currently active")
            self._run["stop_signal"].write_text("stop\n", encoding="utf-8")
            self._run["state"] = "processing"
            self._run["stop_requested_at"] = time.time()
            return self._snapshot_locked()

    def delete_onboard_backup(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            if not self._run or self._run["state"] != "completed":
                raise RuntimeError("recording must complete successfully before deletion")
            if self._run["onboard_deleted"]:
                return self._snapshot_locked()
            run_id = self._run["run_id"]
            match = _RUN_ID.fullmatch(run_id)
            if not match or int(match.group(1)) != self._run["uav_id"]:
                raise RuntimeError("refusing an unsafe onboard path")
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
                text=True,
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise RuntimeError(
                    "onboard backup deletion failed"
                    + (f": {detail}" if detail else "")
                )
            self._run["onboard_deleted"] = True
            return self._snapshot_locked()

    def report_path(self) -> Path:
        with self._lock:
            self._refresh_locked()
            if not self._run or self._run["state"] != "completed":
                raise RuntimeError("the recording report is not ready")
            path = self._run["local_dir"] / "mid360_position_report.html"
            if not path.is_file():
                raise RuntimeError("the recording report file is missing")
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
                    run["log_path"].read_text(encoding="utf-8", errors="replace").splitlines()[-12:]
                )
        except OSError:
            pass
        return {
            "state": run["state"],
            "run_id": run["run_id"],
            "uav_id": run["uav_id"],
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
