"""科目二固定方案离线调整：UAV5移交两点，UAV3从南向北并酌情补扫。"""
from __future__ import annotations

import copy
import itertools
import math

import numpy as np
from shapely import covers, linestrings, set_precision
from shapely.geometry import Point, LineString
from shapely.ops import unary_union, nearest_points

from .landing_avoidance import routes_from_home
from .subject2_budgeted_coverage import _grid
from .subject2_entry_routes import separated_entries


def exclusive_regions(regions, shared, region3, routes):
    """仅调整科目二3/5分界；共边但不共面积，每个扫描点属于自己的子区。"""
    # 统一叠加精度，防止浮点共边产生极细缝隙，被误判为封闭孔洞。
    shared = set_precision(shared, 1e-9)
    region3 = set_precision(region3, 1e-9)
    remaining = shared.difference(region3)
    notches = []
    for point in routes[4]['waypoints_m']:
        if region3.covers(Point(point)):
            # 保留UAV5原航点：向其现有剩余子区开一个15米宽边距的凹口，
            # 避免生成孔洞或孤岛，沿用既有单个外环的任务边界格式。
            if remaining.is_empty:
                raise ValueError('UAV5没有可连通的剩余子区')
            edge = nearest_points(Point(point), remaining)[1]
            notches.append(LineString([point, edge.coords[0]]).buffer(15., cap_style=3, join_style=2))
    region3 = region3.difference(set_precision(unary_union(notches), 1e-9).intersection(shared))
    result = list(regions)
    result[2], result[4] = region3, shared.difference(region3)
    for uid, region in enumerate(result, 1):
        if region.geom_type != 'Polygon' or not region.is_valid or region.interiors or region.is_empty:
            raise ValueError('科目二子区须为有效、连通且无孔洞的多边形')
        if any(not region.contains(Point(p)) for p in routes[uid-1]['waypoints_m']):
            raise ValueError('科目二扫描点不在本机子区内部')
    if any(a.intersection(b).area > 1e-6 for a, b in itertools.combinations(result, 2)):
        raise ValueError('科目二任务子区域存在面积重叠')
    if shared.difference(unary_union([result[2], result[4]])).area > 1e-6:
        raise ValueError('科目二3/5分区遗漏共同固定范围')
    return result


def rebalance(routes, regions, boundary, shared, required, safe, forbidden, depot,
              target, radii, heights, update_timing):
    """只改3/5扫描任务；不增加非扫描中转点，不改变B的逐点正逆执行方式。"""
    source3, source5 = routes[2]['waypoints_m'], routes[4]['waypoints_m']
    if len(source3) != 3 or len(source5) != 8:
        raise ValueError('科目二移交两点需要3点/8点的基准方案')
    pool = np.array([*source3, *source5])
    homes = routes_from_home(boundary, depot, pool.tolist(), [])
    home_lengths = np.array([LineString(p).length for p in homes])
    distances = np.linalg.norm(pool[:, None, :] - pool[None, :, :], axis=2)
    visibility = np.array([covers(safe, linestrings(np.stack(
        [np.tile(p, (len(pool), 1)), pool], axis=1))) for p in pool])
    allowed = safe.intersection(required.buffer(-8.)).difference(forbidden.buffer(radii[3] + .05))
    # 附近点仍必须覆盖用户指定位置，留2米半径余量；精确位置也参与比较。
    near_area = allowed.intersection(Point(target).buffer(radii[3] - 2.))
    near = _grid(near_area, 2.) if not near_area.is_empty else np.empty((0, 2))
    if allowed.covers(Point(target)):
        near = np.vstack([target, near])
    offsets = np.linalg.norm(near - target, axis=1)
    near_distances = np.linalg.norm(pool[:, None, :] - near[None, :, :], axis=2)
    near_visibility = np.array([covers(safe, linestrings(np.stack(
        [np.tile(p, (len(near), 1)), near], axis=1))) for p in pool])

    def shortest(indices, southern=False):
        first = min(indices, key=lambda i: (pool[i, 0], pool[i, 1])) if southern else None
        for order in itertools.permutations(indices):
            if first is not None and order[0] != first:
                continue
            if not all(visibility[a, b] for a, b in zip(order, order[1:])):
                continue
            length = home_lengths[order[0]] + sum(distances[a, b] for a, b in zip(order, order[1:]))
            yield .4 * length + 60 * len(order), order

    candidates = []
    for pair in itertools.combinations(range(3, len(pool)), 2):
        indices3 = (0, 1, 2, *pair)
        if any(not allowed.covers(Point(pool[i])) for i in indices3):
            continue
        if any(distances[a, b] < radii[3] for a, b in itertools.combinations(indices3, 2)):
            continue
        indices5 = tuple(i for i in range(3, len(pool)) if i not in pair)
        best5 = min(shortest(indices5), default=None)
        if best5 is None or best5[0] + heights[5]/6. > 810.:
            continue
        base, optional = None, None
        for duration, order in shortest(indices3, southern=True):
            duration += heights[3]/6.
            if duration <= 810. and (base is None or (duration, order) < base):
                base = duration, order
            expanded = duration + 60. + .4 * near_distances[order[-1]]
            valid = near_visibility[order[-1]] & (expanded <= 810.)
            for index in np.where(valid)[0]:
                option = (float(offsets[index]), float(expanded[index]), order, int(index))
                if optional is None or option < optional:
                    optional = option
        if base is not None:
            candidates.append(dict(pair=pair, order3=base[1], order5=best5[1],
                optional_index=None, offset=math.inf, time3=base[0], time5=best5[0]+heights[5]/6.))
        if optional is not None:
            candidates.append(dict(pair=pair, order3=optional[2], order5=best5[1],
                optional_index=optional[3], offset=optional[0], time3=optional[1], time5=best5[0]+heights[5]/6.))
    # 满足预算时优先完成补扫，再靠近指定点，最后缩短两机总用时。
    candidates.sort(key=lambda c: (c['optional_index'] is None, c['offset'], c['time3']+c['time5']))
    failures = []
    for choice in candidates:
        trial = copy.deepcopy(routes)
        scans3 = pool[list(choice['order3'])].tolist()
        extra = None if choice['optional_index'] is None else near[choice['optional_index']].tolist()
        if extra is not None:
            scans3.append(extra)
        trial[2]['waypoints_m'] = scans3
        trial[4]['waypoints_m'] = pool[list(choice['order5'])].tolist()
        # 先扩展UAV3以包含接手点与补扫点，再重新分界，确保六机子区不重叠。
        points = [Point(pool[i]).buffer(8., quad_segs=1) for i in choice['pair']]
        common3 = unary_union([regions[2], *points]).convex_hull.intersection(shared)
        region3 = (unary_union([common3, Point(extra).buffer(5., quad_segs=1)]).convex_hull
                   .intersection(safe) if extra is not None else common3)
        if region3.geom_type != 'Polygon' or not region3.is_valid:
            continue
        try:
            trial_regions = exclusive_regions(regions, shared, region3, trial)
        except ValueError as error:
            failures.append(str(error))
            continue
        common3 = trial_regions[2].intersection(shared)
        starts = {uid: route['waypoints_m'][0] for uid, route in enumerate(trial, 1)}
        returns = {uid: list(reversed(routes_from_home(boundary, depot, [p], [])[0])) for uid, p in starts.items()}
        limits = {}
        for uid, route in enumerate(trial, 1):
            scans = route['waypoints_m']
            internal = sum(math.dist(a, b) for a, b in zip(scans, scans[1:]))
            limits[uid] = (840.-60*len(scans)-heights[uid]/6.)*5.-2*internal-LineString(returns[uid]).length
        try:
            entries, entry_info = separated_entries(boundary, {uid: depot for uid in starts}, starts,
                reference_home=depot, maximum_lengths=limits)
            for uid, route in enumerate(trial, 1):
                scans = route['waypoints_m']
                if not safe.buffer(1e-7).covers(LineString(scans)):
                    raise ValueError('扫描航段越出安全边界')
                update_timing(route, entries[uid], returns[uid], heights[uid])
                target_region = required.intersection(trial_regions[uid-1])
                disks = unary_union([Point(p).buffer(radii[uid]-.02, quad_segs=64) for p in scans])
                missing = target_region.difference(disks).area
                route.update(scan_waypoints_m=copy.deepcopy(scans), scan_count=len(scans), original_scan_count=len(scans),
                    waypoint_actions=[dict(index=i, action='hover_scan', duration_s=30.) for i in range(len(scans))],
                    required_area_m2=target_region.area, uncovered_area_m2=missing, coverage_ratio=1-missing/target_region.area)
        except ValueError as error:
            failures.append(str(error))
            continue
        disks = [Point(p).buffer(radii[uid]-.02, quad_segs=64)
                 for uid, route in enumerate(trial, 1) for p in route['waypoints_m']]
        covered = unary_union(disks).intersection(required).area
        useful = sum(d.intersection(required).area for d in disks)
        metrics = dict(covered_area_m2=covered, uncovered_area_m2=required.area-covered,
            coverage_ratio=covered/required.area, repeated_scan_area_m2=useful-covered,
            repeated_scan_fraction=(useful-covered)/useful)
        metadata = dict(common_scan_counts={'3': 5, '5': 6}, first_scan_policy='UAV3_southernmost',
            subregions_disjoint=True, shared_boundaries_allowed=True,
            maximum_subregion_overlap_m2=max(a.intersection(b).area
                for a, b in itertools.combinations(trial_regions, 2)),
            transferred_waypoints_m=pool[list(choice['pair'])].tolist(),
            original_priority_polygon_m=list(map(list, list(regions[2].exterior.coords)[:-1])),
            common_uav3_polygon_m=list(map(list, list(common3.exterior.coords)[:-1])),
            requested_optional_point_m=target, optional_included=extra is not None,
            optional_point_m=extra, optional_offset_m=choice['offset'] if extra is not None else None,
            optional_decision='附近合法点可直连，扫描圆覆盖指定位置且六机预算通过' if extra is not None
                else '没有同时满足扫描几何、南侧首点、六机进场分离和时间预算的补扫候选',
            candidates_checked=candidates.index(choice)+1)
        return trial, trial_regions, metrics, entry_info, metadata
    raise ValueError('科目二移交两点后没有合法五点/六点方案：'+('; '.join(failures[-2:]) or '候选不足'))
