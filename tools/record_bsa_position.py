#!/usr/bin/env python3
"""采集视觉或 MID-360 里程计，按 Ctrl+C 结束后生成报告。"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
        return
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    try:
        process.terminate()
        process.wait(timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        process.kill()
        process.wait(timeout=3)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="采集里程计，按 Ctrl+C 结束后生成定位报告。")
    parser.add_argument("--uav-address", default="192.168.1.88")
    parser.add_argument("--remote-user", default="amov")
    parser.add_argument("--source", choices=("vision", "mid360"), default="vision")
    parser.add_argument("--topic", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--report-program", required=True)
    parser.add_argument("--open-report", action="store_true")
    args = parser.parse_args()

    ssh_executable = os.environ.get("BSA_SSH_EXECUTABLE", "ssh")
    if shutil.which(ssh_executable) is None and not Path(ssh_executable).exists():
        raise RuntimeError("未找到 ssh，请先安装 Windows OpenSSH Client。")

    output_root = Path(args.output_root).resolve()
    test_dir = output_root / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{args.source}"
    test_dir.mkdir(parents=True, exist_ok=False)
    raw_path = test_dir / f"{args.source}_odometry.csv"
    report_path = test_dir / f"{args.source}_position_report.html"

    remote_command = (
        "source /opt/ros/noetic/setup.bash; "
        "source /home/amov/su17_experiment/devel/setup.bash; "
        f"topic_type=$(rostopic type {args.topic} 2>/dev/null); "
        f"if [ \"$topic_type\" != \"nav_msgs/Odometry\" ]; then "
        f"echo \"错误：{args.topic} 不是 nav_msgs/Odometry，当前类型=$topic_type\" >&2; exit 20; fi; "
        f"rostopic echo -p {args.topic}"
    )
    command = [ssh_executable, f"{args.remote_user}@{args.uav_address}", remote_command]
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    test_interrupt_after = float(os.environ.get("BSA_TEST_INTERRUPT_AFTER", "0"))

    print(f"测试目录：{test_dir}", flush=True)
    source_display = "BSA SLAM 视觉定位" if args.source == "vision" else "MID-360 FAST-LIO 激光定位"
    print(f"定位源：{source_display}", flush=True)
    print(f"开始采集 {args.remote_user}@{args.uav_address}:{args.topic}", flush=True)
    print("按 Ctrl+C 结束采集；结束后会自动生成 HTML 报表。", flush=True)

    started = time.monotonic()
    interrupted = False
    with raw_path.open("wb") as raw_stream:
        process = subprocess.Popen(
            command,
            stdin=None,
            stdout=raw_stream,
            stderr=None,
            creationflags=creation_flags,
        )
        try:
            last_displayed = -1
            while process.poll() is None:
                elapsed = int(time.monotonic() - started)
                if elapsed != last_displayed:
                    print(f"\r已采集：{format_elapsed(elapsed)}", end="", flush=True)
                    last_displayed = elapsed
                if test_interrupt_after > 0 and time.monotonic() - started >= test_interrupt_after:
                    raise KeyboardInterrupt
                time.sleep(0.1)
        except KeyboardInterrupt:
            interrupted = True
            print(f"\n收到 Ctrl+C，正在停止采集……", flush=True)
            stop_process(process)
        finally:
            if process.poll() is None:
                stop_process(process)

    elapsed_text = format_elapsed(time.monotonic() - started)
    print(f"采集结束，总时长：{elapsed_text}", flush=True)

    if not raw_path.exists() or raw_path.stat().st_size == 0:
        print("没有收到定位数据，请检查网络、ROS 环境和定位话题。", file=sys.stderr)
        return 2
    if not interrupted and process.returncode not in (0, None):
        print(f"SSH 提前结束，退出码：{process.returncode}。仍将尝试处理已收到的数据。", file=sys.stderr)

    print("正在自动分列、分析并生成三维 HTML 报表……", flush=True)
    report_command = [
        sys.executable,
        str(Path(args.report_program).resolve()),
        "--input", str(raw_path),
        "--output-dir", str(test_dir),
        "--source", args.source,
    ]
    completed = subprocess.run(
        report_command,
        check=False,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1"),
    )
    if completed.returncode != 0:
        print(f"报表生成失败，退出码：{completed.returncode}", file=sys.stderr)
        return completed.returncode

    print(f"HTML 报表：{report_path}", flush=True)
    if args.open_report:
        if os.name == "nt":
            os.startfile(report_path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(report_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
