"""程序 B 进返场航线消息；仅做协议校验和高度填充，不控制飞行。"""
import math


def route_messages(assignment):
    routes = assignment['task'].get('transit_routes')
    if routes is None:
        return None
    frame = assignment.get('coordinate_frame', 'WGS84')
    if not isinstance(routes, dict) or routes.get('schema_version') not in (1, 2) or routes.get('coordinate_frame') != frame or frame not in ('WGS84', 'ENU'):
        raise ValueError('进返场航线坐标系或版本不一致')
    version = routes['schema_version']
    recipient = assignment['uav_id']
    altitude = float(assignment['target_altitude_m'])
    def path(values):
        if not isinstance(values, list) or len(values) < 2 or len(values) > 10000:
            raise ValueError('进返场航线须包含起点和终点')
        result = []
        for point in values:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('进返场平面航点格式错误')
            x, y = map(float, point)
            if not all(math.isfinite(v) for v in (x, y, altitude)) or altitude <= 0:
                raise ValueError('进返场航点必须为有效数值')
            if frame == 'WGS84' and (abs(x) > 180 or abs(y) > 90):
                raise ValueError('进返场经纬度越界')
            result.append([x, y, altitude])
        return result
    entry = path(routes['entry_path'])
    returns = routes['return_paths']
    home = path([routes['departure'], routes['departure']])[0]
    if entry[0] != home:
        raise ValueError('进场起点与本机降落点不一致')
    result = []
    scans = assignment['task'].get('waypoints_wgs84') if frame == 'WGS84' else assignment['task']['waypoints_m']
    if not isinstance(scans, list) or not scans or len(scans) != len(assignment['task']['waypoints_m']):
        raise ValueError('进返场航线缺少对应的侦察航点')
    own_points = [[p[1], p[0]] if frame == 'WGS84' else p[:2] for p in scans]
    tolerance = 2e-8 if frame == 'WGS84' else 1e-4
    if version == 2:
        if (type(routes.get('recipient_uav_id')) is not int or routes['recipient_uav_id'] != recipient
                or routes.get('route_scope') != 'all_uav_waypoints'):
            raise ValueError('全机队返航路线接收机或范围不一致')
        landing = assignment.get('landing_point_wgs84' if frame == 'WGS84' else 'landing_point_m')
        if landing is not None:
            expected_home = [landing[1], landing[0]] if frame == 'WGS84' else landing[:2]
            path([expected_home, expected_home])
            if math.dist(home[:2], expected_home) > tolerance:
                raise ValueError('全机队返航路线终点与本机任务降落点不一致')
        catalog = routes.get('waypoints_by_uav')
        if not isinstance(catalog, dict) or set(catalog) != {str(uid) for uid in range(1, 7)}:
            raise ValueError('全机队返航路线必须包含 UAV1～6 的航点')
        for points in catalog.values():
            if not isinstance(points, list) or not 1 <= len(points) <= 10000:
                raise ValueError('全机队侦察航点数量无效')
            for point in points:
                path([point, point])
        if (len(catalog[str(recipient)]) != len(own_points)
                or any(math.dist(a, b) > tolerance for a, b in zip(catalog[str(recipient)], own_points))):
            raise ValueError('全机队航点表与本机侦察任务不一致')
    else:
        catalog = {str(recipient): own_points}
    expected_routes = [(int(uid), i + 1, point) for uid, points in sorted(catalog.items(), key=lambda pair: int(pair[0]))
                       for i, point in enumerate(points)]
    if not isinstance(returns, list) or len(returns) != len(expected_routes):
        raise ValueError('每个侦察航点必须对应一条返航路线')
    own_first = None
    for item, (source, index, expected) in zip(returns, expected_routes):
        if not isinstance(item, dict) or type(item.get('waypoint_index')) is not int or item['waypoint_index'] != index:
            raise ValueError('返航路线须按来源无人机排序且各机航点从 1 连续编号')
        if version == 2 and (type(item.get('source_uav_id')) is not int or item['source_uav_id'] != source):
            raise ValueError('返航路线来源无人机编号不一致或重复')
        values = path(item['path'])
        if math.dist(values[0][:2], expected) > tolerance or values[-1] != home:
            raise ValueError('返航路线起终点与来源侦察航点或本机降落点不一致')
        result.append(dict(waypoint_index=index, path=values))
        if version == 2:
            result[-1]['source_uav_id'] = source
        if source == recipient and index == 1:
            own_first = values[0]
    if entry[-1] != own_first:
        raise ValueError('进场终点必须是本机第一个侦察航点')
    common = dict(schema_version=1, mission_id=assignment['mission_id'], uav_id=assignment['uav_id'],
                  assignment_checksum=assignment['assignment_checksum'], coordinate_frame=frame,
                  coordinate_order='longitude_latitude_relative_altitude' if frame == 'WGS84' else 'x_y_z',
                  altitude_frame='RELATIVE_TO_TAKEOFF', plan_sha256=routes['plan_sha256'])
    if 'boundary_clearance_m' in routes:
        clearance = float(routes['boundary_clearance_m'])
        if not math.isfinite(clearance) or clearance <= 0:
            raise ValueError('进返场边界间距无效')
        if routes.get('endpoint_clearance_policy') != 'takeoff_landing_connector_only':
            raise ValueError('进返场边界间距例外只能用于固定起降点连接段')
        common.update(boundary_clearance_m=clearance,
                      endpoint_clearance_policy=routes['endpoint_clearance_policy'])
    returned = dict(common, routes=result)
    if version == 2:
        returned.update(schema_version=2, route_scope='all_uav_waypoints', landing_point=home,
                        waypoint_counts_by_uav={uid: len(points) for uid, points in catalog.items()})
    return dict(common, path=entry), returned
