"""任务发布端科目一结果去重；只处理上报副本，不改动原始回传。"""

from __future__ import annotations

import bisect
import copy
import datetime as dt
import math


STATIC_DISTANCE_M = 10.0
MOVING_DISTANCE_M = 10.0
MAX_MOVING_TRACK_POINTS = 40
MAX_SUBMISSION_TARGETS = 16


def _confidence(feature):
    value = feature.get("properties", {}).get("confidence")
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return -1.0


def _kind(feature):
    props = feature.get("properties", {})
    model = str(props.get("targetModel") or "").strip().casefold()
    broad = str(props.get("targetType") or "").strip().casefold()
    if model in ("", "未知", "其他"):
        model = broad
    return props.get("targetCategory"), model


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


def _similar_tracks(left, right):
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
        if samples_sorted[7] > MOVING_DISTANCE_M or samples_sorted[-1] > 2 * MOVING_DISTANCE_M:
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
                    "overlap_seconds": round(overlap, 2), "time_offset_seconds": lag}
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


def consolidate(document, *, max_targets=MAX_SUBMISSION_TARGETS):
    """返回去重并按置信度排序的 GeoJSON 及可追溯的合并记录。"""
    result = copy.deepcopy(document)
    raw = result.get("features", [])
    if not isinstance(raw, list):
        return result, {"merged": [], "omitted": [], "raw_count": 0, "result_count": 0}
    ordered = sorted(raw, key=lambda f: (-_confidence(f), str(f.get("id", ""))))
    groups, merged, track_truncations = [], [], []
    for feature in ordered:
        source = feature.pop("_source_uav", None)
        kind = _kind(feature)
        matched = None
        for group in groups:
            if group["kind"] != kind or (source and source in group["sources"]):
                continue
            if kind[0] == "固定":
                coords = feature.get("geometry", {}).get("coordinates")
                distances = [_distance(coords, member.get("geometry", {}).get("coordinates"))
                             for member in group["members"]]
                if distances and max(distances) <= STATIC_DISTANCE_M:
                    matched = (group, {"max_distance_m": round(max(distances), 2)})
                    break
            elif kind[0] == "移动":
                comparisons = [_similar_tracks(feature, member) for member in group["members"]]
                if comparisons and all(similar for similar, _ in comparisons):
                    evidence = max((detail for _, detail in comparisons),
                                   key=lambda detail: detail["mean_distance_m"])
                    # 时间偏移只用于判断同一目标；最终选一条完整的机载轨迹，不拼接多机点。
                    evidence = dict(evidence, track_points_merged=False)
                    matched = (group, evidence)
                    break
        if matched is None:
            groups.append({"feature": feature, "members": [feature], "sources": {source} if source else set(),
                           "kind": kind, "member_sources": [source], "member_evidence": [None]})
            continue
        group, evidence = matched
        group["members"].append(feature)
        group["member_sources"].append(source)
        group["member_evidence"].append(evidence)
        if source:
            group["sources"].add(source)
    features = []
    for group in groups:
        kind = group["kind"]
        if kind[0] == "移动":
            # 不把不同飞机采集的轨迹拼成一条；点数优先，置信度只作同长度裁决。
            group["feature"] = max(group["members"], key=lambda member: (
                len(_track(member)), _confidence(member), str(member.get("id", ""))))
        winner = group["feature"]
        for member, source, evidence in zip(group["members"], group["member_sources"],
                                            group["member_evidence"]):
            if member is winner:
                continue
            if evidence is None:
                if kind[0] == "移动":
                    _, evidence = _similar_tracks(winner, member)
                    evidence = dict(evidence or {}, track_points_merged=False)
                else:
                    evidence = {"max_distance_m": round(_distance(
                        winner.get("geometry", {}).get("coordinates"),
                        member.get("geometry", {}).get("coordinates")), 2)}
            merged.append({"kept_id": winner.get("id"), "merged_id": member.get("id"),
                           "source_uav": source, "target_category": kind[0], "target_type": kind[1],
                           **evidence})
        if kind[0] == "移动":
            original_count = len(winner["properties"].get("trackPoints", []))
            omitted_points = _limit_moving_track(winner)
            if omitted_points:
                track_truncations.append({"id": winner.get("id"), "source_track_points": original_count,
                                          "omitted_track_points": omitted_points})
        features.append(winner)
    features.sort(key=lambda f: (-_confidence(f), str(f.get("id", ""))))
    omitted = [{"id": feature.get("id"), "reason": "超出赛事最多16个目标"}
               for feature in features[max_targets:]]
    features = features[:max_targets]
    seen = set()
    for feature in features:
        original = str(feature.get("id", ""))
        identifier = original
        suffix = 2
        while identifier in seen:
            identifier = "%s-%d" % (original, suffix)
            suffix += 1
        feature["id"] = identifier
        seen.add(identifier)
    result["features"] = features
    return result, {"raw_count": len(raw), "result_count": len(features), "merged": merged,
                    "omitted": omitted, "static_distance_m": STATIC_DISTANCE_M,
                    "moving_distance_m": MOVING_DISTANCE_M,
                    "moving_track_point_limit": MAX_MOVING_TRACK_POINTS,
                    "track_truncations": track_truncations}
