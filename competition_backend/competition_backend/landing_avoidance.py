"""固定比赛区内，按实测起降点重接航线并避开其他起降点。"""
import heapq
import math


def routes_from_home(boundary, home, targets, other_homes, boundary_clearance=5.0, home_clearance=2.5, turning_points=None):
    from shapely.geometry import Point, Polygon, LineString
    from shapely.ops import unary_union
    polygon = Polygon(boundary)
    safe = polygon.buffer(-boundary_clearance - .02, join_style=2)
    if not safe.covers(Point(home)):
        raise ValueError('本科目一起降点距任务外边界不足5米，请调整实际起降位置')
    # 外接正多边形，直线绕过相邻顶点时仍离起降点至少要求的距离。
    radius = (home_clearance + .03) / math.cos(math.pi / 16)
    obstacles = [Point(p).buffer(radius, quad_segs=4) for p in other_homes]
    allowed = safe.difference(unary_union(obstacles))
    parts = [allowed] if allowed.geom_type == 'Polygon' else list(getattr(allowed, 'geoms', []))
    region = next((part for part in parts if part.geom_type == 'Polygon' and part.covers(Point(home))), None)
    if region is None:
        raise ValueError('实际起降点相距过近，无法满足其他降落点2.5米避让要求')
    if any(not region.buffer(1e-8).covers(Point(p)) for p in targets):
        raise ValueError('其他降落点避让区遮挡侦察航点或切断航路，请调整起降位置')
    vertices = [list(p) for ring in (region.exterior, *region.interiors) for p in list(ring.coords)[:-1]]
    if turning_points is not None:
        vertices = [list(p) for p in turning_points if region.buffer(1e-8).covers(Point(p))]
    nodes = [list(home), *vertices]
    graph = [[] for _ in nodes]
    expanded = region.buffer(1e-8)
    for i, a in enumerate(nodes):
        for j in range(i+1, len(nodes)):
            b = nodes[j]
            length = math.dist(a, b)
            if length < 1e-9 or expanded.covers(LineString([a, b])):
                graph[i].append((j, length)); graph[j].append((i, length))
    distances, previous, queue = {0: 0.}, {}, [(0., 0)]
    while queue:
        distance, i = heapq.heappop(queue)
        if distance != distances[i]:
            continue
        for j, length in graph[i]:
            candidate = distance + length
            if candidate + 1e-10 < distances.get(j, math.inf):
                distances[j], previous[j] = candidate, i
                heapq.heappush(queue, (candidate, j))
    paths = []
    # 最短折线路径只需在障碍顶点转弯。所有目标复用同一张图，
    # 不把成百个侦察点两两连边，减少现场分派等待。
    for target in targets:
        visible = [(distances[i] + math.dist(point, target), i)
                   for i, point in enumerate(nodes) if i in distances and
                   (math.dist(point, target) < 1e-9 or expanded.covers(LineString([point, target])))]
        if not visible:
            raise ValueError('比赛进返场航线无法在安全区内连通')
        _, i = min(visible)
        chain = [list(target)]
        while True:
            if math.dist(chain[-1], nodes[i]) > 1e-9:
                chain.append(list(nodes[i]))
            if i == 0:
                break
            i = previous[i]
        path = list(reversed(chain))
        if len(path) == 1:
            path.append(list(path[0]))
        line = LineString(path)
        if (not polygon.covers(line) or line.distance(polygon.boundary) < boundary_clearance
                or any(line.distance(Point(p)) < home_clearance for p in other_homes)):
            raise ValueError('比赛进返场航线最终安全间距校验未通过')
        paths.append(path)
    return paths
