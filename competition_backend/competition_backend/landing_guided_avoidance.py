"""许昌固定通道按本次开机起降点避让；不会修改原科目一离线求解器。"""
import heapq
import math


class _DetourGraph:
    """同一架飞机的全部航段共用障碍可见图；不缓存到下一次规划。"""
    def __init__(self, allowed):
        from shapely.prepared import prep
        self.allowed = prep(allowed.buffer(1e-8))
        parts = [allowed] if allowed.geom_type == 'Polygon' else list(getattr(allowed, 'geoms', []))
        self.vertices = [list(p) for part in parts if part.geom_type == 'Polygon'
                         for ring in (part.exterior, *part.interiors) for p in list(ring.coords)[:-1]]
        self.graph = None
        self.sources = {}

    def visible(self, a, b):
        from shapely.geometry import LineString
        return math.dist(a, b) < 1e-9 or self.allowed.covers(LineString([a, b]))

    def _build(self):
        graph = [[] for _ in self.vertices]
        for i, a in enumerate(self.vertices):
            for j in range(i + 1, len(self.vertices)):
                b = self.vertices[j]
                if self.visible(a, b):
                    length = math.dist(a, b)
                    graph[i].append((j, length))
                    graph[j].append((i, length))
        self.graph = graph

    def route(self, start, target):
        if self.visible(start, target):
            return [list(start), list(target)]
        if self.graph is None:
            self._build()
        key = tuple(start)
        if key not in self.sources:
            distances, previous, queue = {}, {}, []
            for i, point in enumerate(self.vertices):
                if self.visible(start, point):
                    distances[i] = math.dist(start, point)
                    previous[i] = None
                    heapq.heappush(queue, (distances[i], i))
            while queue:
                distance, i = heapq.heappop(queue)
                if distance != distances[i]:
                    continue
                for j, length in self.graph[i]:
                    candidate = distance + length
                    if candidate + 1e-10 < distances.get(j, math.inf):
                        distances[j], previous[j] = candidate, i
                        heapq.heappush(queue, (candidate, j))
            self.sources[key] = distances, previous
        distances, previous = self.sources[key]
        visible = [(distances[i] + math.dist(point, target), i)
                   for i, point in enumerate(self.vertices)
                   if i in distances and self.visible(point, target)]
        if not visible:
            raise ValueError('许昌进返场航线无法在安全区内连通，请调整起降位置')
        _, i = min(visible)
        chain = [list(target)]
        while i is not None:
            if math.dist(chain[-1], self.vertices[i]) > 1e-9:
                chain.append(list(self.vertices[i]))
            i = previous[i]
        if math.dist(chain[-1], start) > 1e-9:
            chain.append(list(start))
        return list(reversed(chain))


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
    planner = _DetourGraph(allowed)
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
                route = planner.route(a, b)
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
