#!/usr/bin/env python3
"""验证 GroundStation 抓包；显式指定 --replay-backend 时才向测试后端回放。"""
import argparse
import base64
import collections
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "competition_backend"))
from competition_backend.groundstation_capture import GroundStationStream, pcapng_ipv4
from competition_backend.pcl_octree import decode_xyz


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pcap", required=True, type=Path, help="pktmon 导出的 pcapng 文件")
    parser.add_argument("--local-ip", default="192.168.1.230", help="Windows 地面网卡地址")
    parser.add_argument("--remote-ip", default="192.168.1.88", help="机载电脑地址")
    parser.add_argument("--remote-port", default=9090, type=int)
    parser.add_argument("--uav-id", type=int, default=3, choices=range(1, 7))
    parser.add_argument("--topic", default="/uav1/octomap_point_cloud_centers/reduce_the_frequency/compressed")
    parser.add_argument("--replay-backend", help="仅测试时指定，如 http://127.0.0.1:18000")
    parser.add_argument("--report", type=Path, help="保存验证结果 JSON")
    args = parser.parse_args()
    stream = GroundStationStream(args.local_ip, args.remote_ip, args.remote_port)
    frames, errors, replayed = [], [], 0
    # 本机回放 HTTP 不使用系统 VPN/HTTP 代理。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for packet in pcapng_ipv4(args.pcap):
        for payload in stream.feed(packet):
            if payload.get("op") != "publish" or not str(payload.get("topic", "")).endswith("/compressed"):
                continue
            try:
                raw = base64.b64decode(payload["msg"]["data"], validate=True)
                body, info = decode_xyz(raw)
                frames.append({"topic": payload["topic"], "points": info["source_points"],
                               "xyz_sha256": hashlib.sha256(body).hexdigest()})
            except (KeyError, ValueError, TypeError) as error:
                errors.append(str(error))
                continue
            if args.replay_backend and payload["topic"] == args.topic:
                payload["_competition_capture"] = "pcap_replay"
                request = urllib.request.Request(
                    args.replay_backend.rstrip("/") + "/api/v1/pointcloud/{}/ingest".format(args.uav_id),
                    data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers={"Content-Type": "application/json",
                             "X-Pointcloud-Token": os.environ.get("COMPETITION_POINTCLOUD_INGEST_TOKEN", "")})
                with opener.open(request, timeout=30) as response:
                    response.read()
                replayed += 1
    report = {"capture": str(args.pcap), "packets": stream.packet_count,
              "complete_messages": stream.message_count, "decoded_frames": len(frames),
              "gap_resets": stream.gap_resets,
              "trailing_bytes": sum(len(s.websocket.buffer) + len(s.websocket.fragment or b"") + s.pending_bytes
                                    for s in stream.flows.values()),
              "errors": errors, "replayed_frames": replayed, "frames": frames}
    counts = collections.Counter(frame["topic"] for frame in frames)
    print("完整消息 {} 条，成功解压 {} 帧，错误 {} 条，回放 {} 帧。".format(
        stream.message_count, len(frames), len(errors), replayed))
    for topic, count in counts.items():
        points = [f["points"] for f in frames if f["topic"] == topic]
        print("{}：{} 帧，每帧 {}～{} 点".format(topic, count, min(points), max(points)))
    print("结尾残帧 {} 字节未作为有效点云提交。".format(report["trailing_bytes"]))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if errors or not frames else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print("抓包验证失败：{}".format(error), file=sys.stderr)
        raise SystemExit(1)
