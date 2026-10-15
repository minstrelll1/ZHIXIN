"""联合生成科目二首航段：不共线，离开起飞集结区后保持水平间距。"""
import heapq
import math

ENTRY_CLEARANCE_M = 3.0
HOME_FAN_RADIUS_M = 30.0


def _route(region, start, end):
    import numpy as np
    from shapely.geometry import Point, LineString
    from shapely import prepare
    from .landing_avoidance import _visible_pairs
    parts = [region] if region.geom_type == 'Polygon' else list(getattr(region, 'geoms', []))
    region = next((p for p in parts if p.geom_type == 'Polygon' and p.covers(Point(start))
                   and p.covers(Point(end))), None)
    if region is None:
        raise ValueError('分离进场通道不连通')
    allowed = region.buffer(1e-8)
    prepare(allowed)
    if allowed.covers(LineString([start, end])):
        return [list(start), list(end)]
    nodes = [list(start), list(end)] + [list(p) for ring in (region.exterior, *region.interiors)
                                      for p in list(ring.coords)[:-1]]
    coordinates = np.asarray(nodes)
    # 按需A*可见图：避免对数百个障碍转角无差别构造平方数量的边。
    heuristic = np.linalg.norm(coordinates-coordinates[1], axis=1)
    distances, previous, queue, settled = {0: 0.}, {}, [(heuristic[0], 0., 0)], set()
    while queue:
        _, distance, a = heapq.heappop(queue)
        if distance != distances[a] or a in settled:
            continue
        if a == 1:
            break
        settled.add(a)
        candidates = np.array([b for b in range(len(nodes)) if b not in settled], dtype=int)
        visible = _visible_pairs(allowed, np.broadcast_to(coordinates[a], (len(candidates),2)), coordinates[candidates])
        for b in candidates[visible]:
            length = math.dist(nodes[a], nodes[b])
            value = distance + length
            if value < distances.get(b, math.inf) - 1e-9:
                distances[b], previous[b] = value, a
                heapq.heappush(queue, (value+heuristic[b], value, b))
    if 1 not in distances:
        raise ValueError('无法生成分离进场通道')
    path, index = [nodes[1]], 1
    while index:
        index = previous[index]; path.append(nodes[index])
    return list(reversed(path))


def separated_entries(boundary, homes, first_points, *, reference_home, maximum_lengths=None):
    """开机起点共同参与计算；相同输入产生相同路线，不依赖发布机或分派顺序。"""
    from shapely.geometry import Point, Polygon, LineString
    from shapely.ops import unary_union
    safe = Polygon(boundary).buffer(-5.05, join_style=2)
    uids = sorted(first_points)
    # 六机共用的离线参考点不可能有正间距；这里只豁免起点周围的扇出区。
    fan = Point(reference_home).buffer(HOME_FAN_RADIUS_M, quad_segs=32)
    for uid in uids:
        if not safe.covers(Point(homes[uid])):
            raise ValueError('UAV%d 起降点距边界不足5米' % uid)
    # 先为远端/北端飞机保留通道，再尝试相反顺序。少量确定性候选避免现场长时间求解。
    preferred = sorted(uids, key=lambda uid: (-first_points[uid][0], uid))
    home_order = sorted(uids, key=lambda uid: (-homes[uid][0], homes[uid][1], uid))
    orders = ([home_order, list(reversed(preferred)), preferred, list(reversed(home_order)), uids, list(reversed(uids))]
              if any(math.dist(homes[uid], homes[uids[0]]) > 1e-6 for uid in uids)
              else [preferred, list(reversed(preferred)), uids, list(reversed(uids))])
    orders += [preferred[i:]+preferred[:i] for i in range(1, len(uids))]
    failure = None
    for order in orders:
        entries, reserved = {}, []
        try:
            for uid in order:
                others = [Point(homes[other]).buffer(2.56/math.cos(math.pi/16), quad_segs=4)
                          for other in uids if other != uid and math.dist(homes[uid], homes[other]) > 1e-6]
                # 其他首航点也预留间距，防止先规划的线挡住后续飞机终点。
                ends = [Point(first_points[other]).buffer(ENTRY_CLEARANCE_M+.08, quad_segs=8)
                        for other in uids if other != uid]
                region = safe.difference(unary_union([*others, *ends, *reserved]))
                path = _route(region, homes[uid], first_points[uid])
                line = LineString(path)
                if maximum_lengths is not None and line.length > maximum_lengths[uid] + 1e-6:
                    raise ValueError('UAV%d 分离进场路线使两趟任务超出14分钟' % uid)
                if any(line.intersection(LineString(previous)).length > 1e-5 for previous in entries.values()):
                    raise ValueError('起飞扇出区内仍有共线航段')
                entries[uid] = path
                reserved.append(line.difference(fan).buffer(ENTRY_CLEARANCE_M+.08, quad_segs=4))
                # 集结区内也不能相交；只有离线共用参考起点的小圆允许六条线扇出。
                thin = line.buffer(.20, quad_segs=2)
                if any(other != uid and math.dist(homes[uid], homes[other]) <= 1e-6 for other in uids):
                    thin = thin.difference(Point(homes[uid]).buffer(1., quad_segs=8))
                reserved.append(thin)
            metrics = validate_entries(boundary, homes, entries, reference_home)
            return entries, metrics
        except ValueError as error:
            failure = error
    raise ValueError('科目二六机首航段无法错开，请调整操场起降位置：%s' % failure)


def validate_entries(boundary, homes, entries, reference_home):
    from shapely.geometry import Point, Polygon, LineString
    polygon = Polygon(boundary)
    fan = Point(reference_home).buffer(HOME_FAN_RADIUS_M, quad_segs=32)
    distances, intersections = {}, {}
    for uid, path in entries.items():
        line = LineString(path)
        if not polygon.covers(line) or line.distance(polygon.boundary) < 5.:
            raise ValueError('科目二进场航线未满足5米边界间距')
        for other, home in homes.items():
            if uid != other and math.dist(homes[uid], home) > 1e-6 and line.distance(Point(home)) < 2.5:
                raise ValueError('科目二进场航线未避开其他降落点')
        for other, path2 in entries.items():
            if uid >= other:
                continue
            second = LineString(path2)
            key = '%d-%d' % (uid, other)
            intersection = line.intersection(second)
            intersections[key] = intersection.length
            distances[key] = line.difference(fan).distance(second.difference(fan))
            shared_origin = math.dist(homes[uid], homes[other]) <= 1e-6
            allowed_contact = Point(homes[uid]).buffer(1e-6) if shared_origin else Point()
            if not intersection.difference(allowed_contact).is_empty:
                raise ValueError('科目二首航段存在交叉')
            if intersections[key] > 1e-5 or distances[key] < ENTRY_CLEARANCE_M - 1e-6:
                raise ValueError('科目二首航段重叠或集结区外间距不足3米')
    return dict(home_fan_radius_m=HOME_FAN_RADIUS_M, entry_clearance_m=ENTRY_CLEARANCE_M,
                pair_distance_outside_fan_m=distances, shared_segment_length_m=intersections,
                scope='entry_only; no intersections except shared preview origin; no dynamic collision guarantee')
