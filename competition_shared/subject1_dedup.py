"""任务发布端科目一结果去重；只处理上报副本，不改动原始回传。"""

from __future__ import annotations

import bisect
import copy
import datetime as dt
import math
from .target_quality import quality_rank


STATIC_DISTANCE_M = 10.0
MOVING_DISTANCE_M = 10.0
OTHER_DISTANCE_M = 5.0
MAX_MOVING_TRACK_POINTS = 40
MAX_SUBMISSION_TARGETS = 16


def _confidence(feature):
    value = feature.get("properties", {}).get("confidence")
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return -1.0


def _quality(feature):
    return quality_rank(feature.get("_quality", {}), _confidence(feature))


def _kind(feature):
    props = feature.get("properties", {})
    raw = str(feature.get("_dedup_category") or props.get("targetType") or "").strip().casefold()
    broad = {"人": "人员", "车": "车辆", "建筑物": "工事",
             "person": "人员", "car": "车辆", "building": "工事"}.get(raw, raw)
    return props.get("targetCategory"), broad


def _pair_distance_limit(left, right):
    a, b = _kind(left), _kind(right)
    same_broad = a[1] == b[1] and a[1] in ("人员", "车辆", "工事")
    moving_person_car = a[0] == b[0] == "移动" and {a[1], b[1]} == {"人员", "车辆"}
    return STATIC_DISTANCE_M if same_broad or moving_person_car else OTHER_DISTANCE_M


def _distance(a, b):
    try:
        lon1, lat1 = map(math.radians, a)
        lon2, lat2 = map(math.radians, b)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))
    except (TypeError, ValueError, OverflowError):
        return math.inf


def _timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _track(feature):
    points = feature.get("properties", {}).get("trackPoints")
    if not isinstance(points, list):
        return []
    valid = []
    for point in points:
        if not isinstance(point, dict):
            continue
        stamp = _timestamp(point.get("timestamp"))
        coords = point.get("coordinates")
        if (stamp is not None and isinstance(coords, list) and len(coords) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in coords)):
            valid.append((stamp, coords))
    return sorted(valid, key=lambda value: value[0])


def _position_at(track, stamps, when):
    index = bisect.bisect_left(stamps, when)
    if index == 0:
        return track[0][1]
    if index >= len(track):
        return track[-1][1]
    before, after = track[index - 1], track[index]
    span = after[0] - before[0]
    if span <= 0:
        return after[1]
    fraction = (when - before[0]) / span
    return [before[1][axis] + (after[1][axis] - before[1][axis]) * fraction for axis in (0, 1)]


def _similar_tracks(left, right, distance_m=None):
    distance_m = _pair_distance_limit(left, right) if distance_m is None else distance_m
    a, b = _track(left), _track(right)
    if len(a) < 2 or len(b) < 2:
        return False, None
    a_duration, b_duration = a[-1][0] - a[0][0], b[-1][0] - b[0][0]
    if min(a_duration, b_duration) < 3.0:
        return False, None
    a_stamps, b_stamps = [p[0] for p in a], [p[0] for p in b]
    best = None
    # 同一物体经不同链路上报可能错开数秒；只容许少量时间偏移。
    for lag in range(-3, 4):
        start, end = max(a[0][0], b[0][0] + lag), min(a[-1][0], b[-1][0] + lag)
        overlap = end - start
        if overlap < max(3.0, min(a_duration, b_duration) * 0.5):
            continue
        samples = []
        first_a = first_b = last_a = last_b = None
        for index in range(9):
            moment = start + overlap * index / 8
            pos_a = _position_at(a, a_stamps, moment)
            pos_b = _position_at(b, b_stamps, moment - lag)
            samples.append(_distance(pos_a, pos_b))
            if index == 0:
                first_a, first_b = pos_a, pos_b
            if index == 8:
                last_a, last_b = pos_a, pos_b
        samples_sorted = sorted(samples)
        if samples_sorted[7] > distance_m or samples_sorted[-1] > 2 * distance_m:
            continue
        # 相邻车道反向通行不能只凭靠得近而合并。
        travel_a, travel_b = _distance(first_a, last_a), _distance(first_b, last_b)
        if travel_a > 5 and travel_b > 5:
            scale = math.cos(math.radians((first_a[1] + last_a[1]) / 2))
            va = ((last_a[0] - first_a[0]) * scale, last_a[1] - first_a[1])
            vb = ((last_b[0] - first_b[0]) * scale, last_b[1] - first_b[1])
            norm = math.hypot(*va) * math.hypot(*vb)
            if norm and (va[0] * vb[0] + va[1] * vb[1]) / norm < 0.7:
                continue
        mean = sum(samples) / len(samples)
        if best is None or mean < best["mean_distance_m"]:
            best = {"mean_distance_m": round(mean, 2), "max_distance_m": round(samples_sorted[-1], 2),
                    "overlap_seconds": round(overlap, 2), "time_offset_seconds": lag,
                    "distance_limit_m": distance_m}
    return best is not None, best


def _limit_moving_track(feature):
    """保留单条轨迹中按目标时间排序的最早 40 个有效点。"""
    props = feature["properties"]
    points = sorted(props.get("trackPoints", []), key=lambda point: _timestamp(point["timestamp"]))
    original_count = len(points)
    selected = points[:MAX_MOVING_TRACK_POINTS]
    if len(selected) >= 2:
        props["trackPoints"] = selected
        props["trackStartTime"] = selected[0]["timestamp"]
        props["trackEndTime"] = selected[-1]["timestamp"]
        feature["geometry"]["coordinates"] = [point["coordinates"] for point in selected]
    return original_count - len(selected)


def _pair_match(left, right):
    """对每一对候选分别应用混淆组距离，避免链式合并越过较小阈值。"""
    a, b = _kind(left), _kind(right)
    limit = _pair_distance_limit(left, right)
    if a[0] != b[0]:
        return False, None
    if a[0] == "移动":
        same, detail = _similar_tracks(left, right, limit)
        return same, dict(detail, track_points_merged=False) if detail else None
    if a[0] == "固定":
        distance = _distance(left.get("geometry", {}).get("coordinates"),
                             right.get("geometry", {}).get("coordinates"))
        return distance <= limit, dict(max_distance_m=round(distance, 2), distance_limit_m=limit)
    return False, None


def _ordered(features):
    return sorted(features, key=lambda f: (tuple(-v for v in _quality(f)),
                                          str(f.get("id", "")), str(f.get("_source_uav", ""))))


def consolidate(document, *, max_targets=MAX_SUBMISSION_TARGETS):
    """同机和跨机按混淆组判重；有效候选达到上限时从合并候选补足要求数量。"""
    if type(max_targets) is not int or max_targets < 1:
        raise ValueError("上报目标数量必须是正整数")
    result = copy.deepcopy(document)
    raw = result.get("features", [])
    if not isinstance(raw, list):
        return result, {"merged": [], "omitted": [], "backfilled": [], "raw_count": 0, "result_count": 0}
    groups, merged, track_truncations = [], [], []
    for feature in _ordered(raw):
        matched = None
        for group in groups:
            comparisons = [_pair_match(feature, member) for member in group["members"]]
            if comparisons and all(same for same, _ in comparisons):
                matched = group
                break
        if matched is None:
            groups.append({"members": [feature]})
        else:
            matched["members"].append(feature)

    winners, merged_candidates, represented_by = [], [], {}
    for group in groups:
        if all(_kind(member)[0] == "移动" for member in group["members"]):
            winner = max(group["members"], key=lambda member: (
                min(MAX_MOVING_TRACK_POINTS, len(_track(member))), _quality(member), str(member.get("id", ""))))
        else:
            winner = _ordered(group["members"])[0]
        winners.append(winner)
        for member in group["members"]:
            if member is winner:
                continue
            _, evidence = _pair_match(winner, member)
            merged_candidates.append(member)
            represented_by[id(member)] = winner
            merged.append({"kept_id": winner.get("id"), "merged_id": member.get("id"),
                           "kept_source_uav": winner.get("_source_uav"),
                           "source_uav": member.get("_source_uav"),
                           "same_uav": bool(member.get("_source_uav")) and member.get("_source_uav") == winner.get("_source_uav"),
                           "target_category": _kind(member)[0], "target_type": _kind(member)[1],
                           "kept_target_type": _kind(winner)[1],
                           "kept_quality": winner.get("_quality", {}),
                           "merged_quality": member.get("_quality", {}), **(evidence or {})})

    selected = _ordered(winners)[:max_targets]
    backfilled = []
    if len(raw) >= max_targets and len(selected) < max_targets:
        for candidate in _ordered(merged_candidates)[:max_targets-len(selected)]:
            selected.append(candidate)
            winner = represented_by[id(candidate)]
            backfilled.append({"id": candidate.get("id"), "source_uav": candidate.get("_source_uav"),
                               "represented_by": winner.get("id"),
                               "representative_source_uav": winner.get("_source_uav"),
                               "quality": candidate.get("_quality", {}), "score": _confidence(candidate),
                               "reason": "有效候选达到要求数量，按质量从合并候选补足上报数量"})
    features = _ordered(selected)
    selected_ids = {id(feature) for feature in features}
    omitted = [{"id": feature.get("id"), "source_uav": feature.get("_source_uav"),
                "reason": "超出赛事最多16个目标"} for feature in _ordered(winners) if id(feature) not in selected_ids]
    for feature in features:
        if _kind(feature)[0] == "移动":
            count = len(feature["properties"].get("trackPoints", []))
            removed = _limit_moving_track(feature)
            if removed:
                track_truncations.append({"id": feature.get("id"), "source_uav": feature.get("_source_uav"),
                                          "source_track_points": count, "omitted_track_points": removed})
    quality_ranking = [{"id": f.get("id"), "source_uav": f.get("_source_uav"),
                        **f.get("_quality", {}), "score": _confidence(f)} for f in features]
    seen = set()
    for feature in features:
        feature.pop("_quality", None)
        feature.pop("_source_uav", None)
        feature.pop("_dedup_category", None)
        original = str(feature.get("id", ""))
        identifier, suffix = original, 2
        while identifier in seen:
            identifier = "%s-%d" % (original, suffix)
            suffix += 1
        feature["id"] = identifier
        seen.add(identifier)
    result["features"] = features
    return result, {"dedup_scope": "within_and_across_uavs",
                    "raw_count": len(raw), "deduplicated_count": len(winners), "result_count": len(features),
                    "merged": merged, "backfilled": backfilled, "backfilled_count": len(backfilled),
                    "omitted": omitted, "static_distance_m": STATIC_DISTANCE_M,
                    "moving_distance_m": MOVING_DISTANCE_M, "other_distance_m": OTHER_DISTANCE_M,
                    "moving_track_point_limit": MAX_MOVING_TRACK_POINTS,
                    "track_truncations": track_truncations, "quality_ranking": quality_ranking,
                    "quality_order": ["tracking_success", "detection_count", "score"]}
