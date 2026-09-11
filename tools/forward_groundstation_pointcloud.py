#!/usr/bin/env python3
"""Forward existing GroundStation ROS PointCloud2 topics to the web backend.

Run on the PrometheusGroundStation computer where the topic already exists in
its local ROS graph.  This program creates no ROSBridge connection to a UAV;
it only POSTs to the competition backend's existing HTTP port (normally 8000).
"""
from __future__ import annotations

import argparse
import base64
import json
import queue
import threading
import time
from typing import Dict, Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen

import rospy
from sensor_msgs.msg import PointCloud2


def parse_topics(value: str) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for entry in value.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        key, separator, topic = entry.partition("=")
        if not separator or not topic.strip().startswith("/"):
            raise argparse.ArgumentTypeError(
                "--uav-topics uses UAV_ID=/ros/topic;..."
            )
        uav_id = int(key.strip())
        if uav_id not in range(1, 7):
            raise argparse.ArgumentTypeError("UAV ID must be between 1 and 6")
        result[uav_id] = topic.strip()
    if not result:
        raise argparse.ArgumentTypeError("at least one UAV topic is required")
    return result


def envelope(topic: str, message: PointCloud2) -> Dict[str, object]:
    stamp = message.header.stamp
    return {
        "op": "publish",
        "topic": topic,
        "msg": {
            "header": {
                "frame_id": message.header.frame_id,
                "stamp": {"secs": int(stamp.secs), "nsecs": int(stamp.nsecs)},
            },
            "height": int(message.height),
            "width": int(message.width),
            "fields": [
                {
                    "name": field.name,
                    "offset": int(field.offset),
                    "datatype": int(field.datatype),
                    "count": int(field.count),
                }
                for field in message.fields
            ],
            "is_bigendian": bool(message.is_bigendian),
            "point_step": int(message.point_step),
            "row_step": int(message.row_step),
            "data": base64.b64encode(bytes(message.data)).decode("ascii"),
            "is_dense": bool(message.is_dense),
        },
    }


def post_frame(url: str, token: str, payload: Dict[str, object]) -> None:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Pointcloud-Token"] = token
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=8) as response:
        if response.status // 100 != 2:
            raise URLError("HTTP {}".format(response.status))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uav-topics", required=True, type=parse_topics)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--hz", type=float, default=1.0)
    args = parser.parse_args()
    period = 1.0 / max(0.1, args.hz)
    pending: "queue.Queue[Tuple[int, str, PointCloud2]]" = queue.Queue(maxsize=1)
    last_sent = {uav_id: 0.0 for uav_id in args.uav_topics}

    def receiver(uav_id: int, topic: str):
        def callback(message: PointCloud2) -> None:
            now = time.monotonic()
            if now - last_sent[uav_id] < period:
                return
            last_sent[uav_id] = now
            item = (uav_id, topic, message)
            try:
                pending.put_nowait(item)
            except queue.Full:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    pass
                pending.put_nowait(item)
        return callback

    def sender() -> None:
        base = args.backend.rstrip("/")
        while not rospy.is_shutdown():
            try:
                uav_id, topic, message = pending.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                post_frame(
                    "{}/api/v1/pointcloud/{}/ingest".format(base, uav_id),
                    args.token,
                    envelope(topic, message),
                )
            except (OSError, URLError, ValueError) as error:
                rospy.logwarn_throttle(5, "点云转发失败：%s", error)

    rospy.init_node("competition_groundstation_pointcloud_forwarder", anonymous=True)
    threading.Thread(target=sender, name="pointcloud-http-forwarder", daemon=True).start()
    for uav_id, topic in args.uav_topics.items():
        rospy.Subscriber(
            topic,
            PointCloud2,
            receiver(uav_id, topic),
            queue_size=1,
            buff_size=64 * 1024 * 1024,
        )
        rospy.loginfo("正在共享 GroundStation 点云 UAV%s：%s", uav_id, topic)
    rospy.spin()


if __name__ == "__main__":
    main()
