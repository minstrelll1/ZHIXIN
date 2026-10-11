"""许昌固定通道按本次开机起降点避让；不会修改原科目一离线求解器。"""
import math
from .landing_avoidance import routes_from_home


def detour_guided_paths(boundary, paths, other_homes, *, holes=(),
                        boundary_clearance=5.0, home_clearance=2.5):
    """保留既有分机通道，只绕开与其他降落点冲突的航段；共享航段复用结果。"""
    from shapely.geometry import Point, Polygon, LineString
    from shapely.ops import unary_union
    polygon = Polygon(boundary, holes=holes)
    radius = (home_clearance + .03) / math.cos(math.pi / 16)
    allowed = polygon.buffer(-boundary_clearance-.02, join_style=2).difference(
        unary_union([Point(p).buffer(radius, quad_segs=4) for p in other_homes]))
    expanded = allowed.buffer(1e-8)
    cache, result = {}, []
    for original in paths:
        if (len(original) < 2 or not expanded.covers(Point(original[0]))
                or not expanded.covers(Point(original[-1]))):
            raise ValueError('实际起降点或侦察航点与其他降落点2.5米避让区冲突，请调整起降位置')
        # 门点/绕障拐点仅为路线导引，不是侦察点；被新障碍覆盖时绕过该导引点。
        guides = [original[0], *[p for p in original[1:-1] if expanded.covers(Point(p))], original[-1]]
        routed = [list(guides[0])]
        for a, b in zip(guides, guides[1:]):
            if math.dist(a, b) < 1e-9:
                continue
            key = (tuple(a), tuple(b))
            if key not in cache:
                route = ([list(a), list(b)] if expanded.covers(LineString([a,b])) else
                         routes_from_home(polygon, a, [b], other_homes,
                                          boundary_clearance, home_clearance)[0])
                cache[key] = route
                cache[(tuple(b),tuple(a))] = list(reversed(route))
            routed.extend(cache[key][1:])
        if len(routed) == 1:
            routed.append(list(routed[0]))
        line = LineString(routed)
        if (not polygon.covers(line) or line.distance(polygon.boundary) < boundary_clearance
                or any(line.distance(Point(p)) < home_clearance for p in other_homes)):
            raise ValueError('许昌进返场航线最终5米边界/2.5米降落点间距校验未通过')
        result.append(routed)
    return result
