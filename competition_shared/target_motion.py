"""回传结果的动静复核；只影响地面赛事副本，不改变机载原始消息。"""

import math

from .subject1_dedup import _distance, _timestamp


MIN_MOVING_SPEED_KMH = 3.0
MIN_MOVING_SPEED_MPS = MIN_MOVING_SPEED_KMH / 3.6
STATIONARY_MIN_SECONDS = 15.0
LOW_SPEED_TIME_FRACTION = 0.7


def classify_motion(first_metadata, points):
    """首次为动目标时，按完整定位历史各区间的时长加权复核。

    有效观测至少15秒，低于3 km/h的区间占有效时长严格超过70%才转静态。
    速度用相邻WGS84定位的路程/定位时差计算，不用首尾净位移或点数投票。
    等待、补传和重复定位时间不增加观察时长；稀疏样本单独标记。
    """
    first_moving = bool(first_metadata.get("is_moving", False))
    audit = dict(first_report_is_moving=first_moving,
                 reported_is_moving=first_moving, rule="first_onboard_receipt",
                 minimum_moving_speed_kmh=MIN_MOVING_SPEED_KMH,
                 minimum_moving_speed_mps=MIN_MOVING_SPEED_MPS,
                 stationary_min_seconds=STATIONARY_MIN_SECONDS,
                 low_speed_fraction_threshold=LOW_SPEED_TIME_FRACTION,
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

    low_distance = low_duration = duration = max_speed = max_gap = 0.0
    low_intervals = 0
    for before, after in zip(track, track[1:]):
        gap = after[0] - before[0]
        speed = _distance(before[2], after[2]) / gap
        duration += gap
        max_gap = max(max_gap, gap)
        # 浮点容差使恰好3 km/h仍归入非低速区间。
        if speed < MIN_MOVING_SPEED_MPS - 1e-9:
            low_duration += gap
            low_distance += speed * gap
            low_intervals += 1
            max_speed = max(max_speed, speed)
    fraction = low_duration / duration
    audit.update(valid_duration_seconds=round(duration, 6),
                 low_speed_duration_seconds=round(low_duration, 6),
                 low_speed_time_fraction=round(fraction, 9),
                 low_speed_interval_count=low_intervals,
                 valid_interval_count=len(track) - 1,
                 low_speed_distance_m=round(low_distance, 6),
                 low_speed_max_segment_mps=round(max_speed, 6),
                 low_speed_average_mps=round(low_distance / low_duration, 6) if low_duration else None,
                 observation_start_time=track[0][1], observation_end_time=track[-1][1],
                 max_observation_gap_seconds=round(max_gap, 6),
                 sparse_observations=max_gap > STATIONARY_MIN_SECONDS,
                 speed_source="WGS84_distance_over_localization_time")
    if duration >= STATIONARY_MIN_SECONDS and fraction > LOW_SPEED_TIME_FRACTION + 1e-12:
        audit.update(reported_is_moving=False, rule="majority_low_speed",
                     converted_to_static=True,
                     reason="有效观测至少15秒且低于3 km/h的时长超过70%，按最后回传整条记录上报固定目标")
        return False, audit
    audit["reason"] = ("有效观察不足15秒，保留首次动态分类" if duration < STATIONARY_MIN_SECONDS else
                       "低于3 km/h的时长未超过70%，保留首次动态分类")
    return True, audit
