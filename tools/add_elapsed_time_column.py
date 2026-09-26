#!/usr/bin/env python3
"""为飞行记录目录中的 CSV 增加统一的相对时间列。

相对时间以目录内所有 CSV 的最早 ``%time`` 为零点，格式为 ``X分YY.ss秒``，
例如 ``1分27.05秒``，并放在第二列。脚本按文本行处理，保留 rosbag
``rostopic echo -p`` 产生的原始内容，包括个别消息字段中未转义的逗号。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


COLUMN_NAME = "elapsed_min_sec"


def _timestamp_ns(line: str) -> int | None:
    """读取 CSV 行首的 ``%time``（ROS 记录时间，单位纳秒）。"""
    first = line.split(",", 1)[0].strip()
    if not first or first == "%time":
        return None
    try:
        value = float(first)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def _with_newline(content: str, original: str) -> str:
    if original.endswith("\r\n"):
        return content + "\r\n"
    if original.endswith("\n"):
        return content + "\n"
    if original.endswith("\r"):
        return content + "\r"
    return content


def _append(line: str, value: str) -> str:
    newline = ""
    content = line
    if content.endswith("\r\n"):
        newline, content = "\r\n", content[:-2]
    elif content.endswith(("\n", "\r")):
        newline, content = content[-1], content[:-1]
    return content + ',"' + value + '"' + newline


def _remove_last(line: str) -> str:
    """删除末尾旧计时列，保留前面可能含有逗号的原始消息文本。"""
    newline = ""
    content = line
    if content.endswith("\r\n"):
        newline, content = "\r\n", content[:-2]
    elif content.endswith(("\n", "\r")):
        newline, content = content[-1], content[:-1]
    comma = content.rfind(",")
    return (content[:comma] if comma >= 0 else content) + newline


def _insert_second(line: str, value: str) -> str:
    """把计时列插入第二列，不拆解后面的自由文本字段。"""
    newline = ""
    content = line
    if content.endswith("\r\n"):
        newline, content = "\r\n", content[:-2]
    elif content.endswith(("\n", "\r")):
        newline, content = content[-1], content[:-1]
    comma = content.find(",")
    if comma < 0:
        return content + ',"' + value + '"' + newline
    return content[: comma + 1] + '"' + value + '",' + content[comma + 1 :] + newline


def _format_elapsed(delta_ns: int) -> str:
    """格式化为整数分钟和百分之一秒，例如 ``1分27.05秒``。"""
    centiseconds = max(0, int((int(delta_ns) + 5_000_000) // 10_000_000))
    minutes, remainder = divmod(centiseconds, 6000)
    return f"{minutes}分{remainder // 100:02d}.{remainder % 100:02d}秒"


def add_elapsed_columns(directory: Path, recompute: bool = False) -> tuple[int, int, list[str]]:
    paths = sorted(directory.glob("*.csv"))
    if not paths:
        raise FileNotFoundError(f"目录中没有 CSV 文件：{directory}")

    contents: dict[Path, list[str]] = {}
    baseline_ns: int | None = None
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines(keepends=True)
        contents[path] = lines
        if not lines:
            continue
        for line in lines[1:]:
            timestamp = _timestamp_ns(line)
            if timestamp is not None:
                baseline_ns = timestamp if baseline_ns is None else min(baseline_ns, timestamp)
                break

    if baseline_ns is None:
        raise ValueError(f"CSV 中没有可用的 %time：{directory}")

    changed = 0
    rows = 0
    failed: list[str] = []
    for path, lines in contents.items():
        if not lines or (COLUMN_NAME in lines[0] and not recompute):
            continue
        has_column = COLUMN_NAME in lines[0]
        updated = [_insert_second(_remove_last(lines[0]), COLUMN_NAME) if has_column
                   else _insert_second(lines[0], COLUMN_NAME)]
        for line in lines[1:]:
            if not line.strip():
                updated.append(line)
                continue
            timestamp = _timestamp_ns(line)
            elapsed = ""
            if timestamp is not None:
                elapsed = _format_elapsed(timestamp - baseline_ns)
                rows += 1
            updated.append(_insert_second(_remove_last(line), elapsed) if has_column
                           else _insert_second(line, elapsed))
        try:
            # 写入 UTF-8 BOM，Windows Excel 可自动识别中文，而不需要手动选择编码。
            path.write_text("".join(updated), encoding="utf-8-sig", newline="")
        except OSError as error:
            # 一个 CSV 可能仍被地面服务或预览程序打开；不要因此中断同一批次的其他文件。
            failed.append(f"{path.name}: {error}")
            continue
        changed += 1
    return changed, rows, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="为飞行记录 CSV 添加统一的分秒相对时间列。")
    parser.add_argument("--directory", required=True, type=Path, help="飞行记录目录")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="重新按目录内最早 %time 覆盖已有计时列",
    )
    args = parser.parse_args()
    changed, rows, failed = add_elapsed_columns(args.directory.resolve(), args.recompute)
    print(f"已添加 {COLUMN_NAME}：{changed} 个 CSV，{rows} 行数据。")
    if failed:
        for item in failed:
            print(f"未能写入（文件可能被占用）：{item}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
