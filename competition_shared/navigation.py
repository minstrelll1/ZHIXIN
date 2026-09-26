"""WGS84 地点转换到起飞时记录的本地 ENU；不把地图原点当作飞控原点。"""
import math


def wgs84_to_enu_xy(latitude, longitude, origin_latitude, origin_longitude):
    values = [float(v) for v in (latitude, longitude, origin_latitude, origin_longitude)]
    if not all(math.isfinite(v) for v in values) or any(abs(v) > 90 for v in values[::2]) or any(abs(v) > 180 for v in values[1::2]):
        raise ValueError('WGS84 经纬度无效')
    lat, lon, lat0, lon0 = map(math.radians, values)
    a, e2 = 6378137.0, 6.69437999014e-3
    def ecef(phi, lam):
        n = a / math.sqrt(1 - e2 * math.sin(phi)**2)
        return n * math.cos(phi) * math.cos(lam), n * math.cos(phi) * math.sin(lam), n * (1-e2) * math.sin(phi)
    p, q = ecef(lat, lon), ecef(lat0, lon0)
    dx, dy, dz = [p[i] - q[i] for i in range(3)]
    return [-math.sin(lon0)*dx + math.cos(lon0)*dy,
            -math.sin(lat0)*math.cos(lon0)*dx - math.sin(lat0)*math.sin(lon0)*dy + math.cos(lat0)*dz]


def resolve_waypoints(task, home, gps_home, max_distance):
    if task['coordinate_frame'] == 'ENU':
        points = [list(p) for p in task['waypoints_m']]
    elif task['coordinate_frame'] == 'LOCAL_NORTH_WEST':
        if gps_home is None or home is None:
            raise ValueError('地图任务必须先记录有效 GPS 和本地 ENU 起飞锚点')
        points = []
        for lat, lon, *rest in task['waypoints_wgs84']:
            east, north = wgs84_to_enu_xy(lat, lon, gps_home[0], gps_home[1])
            points.append([home[0]+east, home[1]+north])
    else:
        raise ValueError('不支持的任务坐标系')
    anchor = home or (0, 0)
    if any(math.hypot(p[0]-anchor[0], p[1]-anchor[1]) > max_distance for p in points):
        raise ValueError('航点超出所配置的离家距离上限')
    return points
