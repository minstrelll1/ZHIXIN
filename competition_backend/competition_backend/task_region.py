"""从最终分区生成机载边界；共用任务航点的坐标变换，保留扣除区。"""
import math
from shapely.geometry import Polygon
from shapely.ops import unary_union
from competition_shared.task_region import validate_task_region


def build_task_region(task, area, uav_id):
    polygon = task.get("polygon_m")
    if not polygon:
        bounds = task.get("bounds_m") or {}
        if not all(k in bounds for k in ("x_min", "x_max", "y_min", "y_max")):
            return None
        polygon = [[bounds["x_min"], bounds["y_min"]], [bounds["x_max"], bounds["y_min"]],
                   [bounds["x_max"], bounds["y_max"]], [bounds["x_min"], bounds["y_max"]]]
    geometry = Polygon(polygon)
    exclusions = (area or {}).get("excluded_polygons_m") or []
    if exclusions:
        geometry = geometry.difference(unary_union([Polygon(ring) for ring in exclusions]))
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("任务子区域几何无效")
    points, gps = task.get("waypoints_m"), task.get("waypoints_wgs84")
    use_gps = (area or {}).get("coordinate_mode") == "gps" or task.get("coordinate_frame") == "LOCAL_NORTH_WEST"
    convert = lambda p: [float(p[0]), float(p[1])]
    if use_gps:
        if not points or not gps or len(points) != len(gps):
            raise ValueError("任务子区域缺少对应 GPS 参考")
        projection = (area or {}).get("coverage", {}).get("projection") or {}
        factors = [1 / float(projection.get("north_m_per_degree", 111111.0)),
                   -1 / float(projection.get("west_m_per_degree", 111111.0 * max(1e-6, abs(math.cos(math.radians(gps[0][0]))))))]
        for axis in (0, 1):
            lo = min(range(len(points)), key=lambda i: points[i][axis])
            hi = max(range(len(points)), key=lambda i: points[i][axis])
            span = points[hi][axis] - points[lo][axis]
            if abs(span) > 1e-6:
                factors[axis] = (gps[hi][axis] - gps[lo][axis]) / span
        def convert(p):
            return [gps[0][1] + (p[1] - points[0][1]) * factors[1],
                    gps[0][0] + (p[0] - points[0][0]) * factors[0]]
        if any(math.dist(convert(p), [g[1], g[0]]) > 2e-8 for p, g in zip(points, gps)):
            raise ValueError("任务子区域与 GPS 航点投影不一致")
    parts = [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)
    result = dict(schema_version=1, uav_id=int(uav_id), coordinate_frame="WGS84" if use_gps else "ENU",
                  coordinate_order="longitude_latitude" if use_gps else "x_y", polygons=[])
    for part in parts:
        if part.geom_type != "Polygon":
            raise ValueError("任务子区域必须为多边形")
        result["polygons"].append(dict(outer=[convert(p) for p in part.exterior.coords],
                                       holes=[[convert(p) for p in ring.coords] for ring in part.interiors]))
    return validate_task_region(result, int(uav_id))
