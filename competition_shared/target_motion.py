"""回传结果的动静复核；只影响地面赛事副本，不改变机载原始消息。"""

import math

from .subject1_dedup import _distance, _timestamp


MIN_MOVING_SPEED_KMH = 4.0
MIN_MOVING_SPEED_MPS = MIN_MOVING_SPEED_KMH / 3.6
STATIONARY_MIN_SECONDS = 15.0


def classify_motion(first_metadata, points):
    """先按首次属性分类，再复核末段有效定位；返回分类与可追溯依据。

    末段每一对相邻、不同定位时刻的距离/时差都须小于4 km/h，累计
    跨度至少15秒。不能用首尾净位移掩盖中途高速移动或折返；等待、
    补传和重复定位时间均不会增加观察时长。稀疏样本只代表观测均速，
    单独记录最大间隔，不能证明未观测期间完全静止。
    """
    first_moving = bool(first_metadata.get("is_moving", False))
    audit = dict(first_report_is_moving=first_moving,
                 reported_is_moving=first_moving, rule="first_onboard_receipt",
                 minimum_moving_speed_kmh=MIN_MOVING_SPEED_KMH,
                 minimum_moving_speed_mps=MIN_MOVING_SPEED_MPS,
                 stationary_min_seconds=STATIONARY_MIN_SECONDS,
                 converted_to_static=False)
    if not first_moving:
        return False, audit

    unique = {}
    for point in points:
        stamp = _timestamp(point.get("timestamp"))
        coords = point.get("coordinates")
        if (stamp is not None and isinstance(coords, (list, tuple)) and len(coords) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in coords)
                and -180 <= coords[0] <= 180 and -90 <= coords[1] <= 90):
            unique[stamp] = (point["timestamp"], coords)
    track = sorted((stamp, *value) for stamp, value in unique.items())
    audit["valid_position_count"] = len(track)
    if len(track) < 2:
        audit["reason"] = "有效定位不足两个，不能证明持续低速"
        return True, audit

    distance = duration = max_speed = max_gap = 0.0
    start = len(track) - 1
    for index in range(len(track) - 1, 0, -1):
        before, after = track[index - 1], track[index]
        gap = after[0] - before[0]
        travel = _distance(before[2], after[2])
        speed = travel / gap
        if speed >= MIN_MOVING_SPEED_MPS - 1e-9:
            break
        distance += travel
        duration += gap
        max_speed = max(max_speed, speed)
        max_gap = max(max_gap, gap)
        start = index - 1
    audit.update(low_speed_duration_seconds=round(duration, 6),
                 low_speed_distance_m=round(distance, 6),
                 low_speed_max_segment_mps=round(max_speed, 6),
                 low_speed_average_mps=round(distance / duration, 6) if duration else None,
                 low_speed_observation_count=len(track) - start,
                 low_speed_start_time=track[start][1], low_speed_end_time=track[-1][1],
                 max_observation_gap_seconds=round(max_gap, 6),
                 sparse_observations=max_gap > STATIONARY_MIN_SECONDS,
                 speed_source="WGS84_distance_over_localization_time")
    if duration >= STATIONARY_MIN_SECONDS:
        audit.update(reported_is_moving=False, rule="sustained_low_speed",
                     converted_to_static=True,
                     reason="末段定位连续区间均速低于4 km/h且累计至少15秒，按最后回传整条记录上报固定目标")
        return False, audit
    audit["reason"] = "末段低速观察不足15秒，保留首次动态分类"
    return True, audit
