"""程序 B 进返场航线消息；仅做协议校验和高度填充，不控制飞行。"""
import math


def route_messages(assignment):
    routes = assignment['task'].get('transit_routes')
    if routes is None:
        return None
    frame = assignment.get('coordinate_frame', 'WGS84')
    if not isinstance(routes, dict) or routes.get('schema_version') != 1 or routes.get('coordinate_frame') != frame or frame not in ('WGS84', 'ENU'):
        raise ValueError('进返场航线坐标系或版本不一致')
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
    if not isinstance(returns, list) or len(returns) != len(assignment['task']['waypoints_m']):
        raise ValueError('每个侦察航点必须对应一条返航路线')
    home = path([routes['departure'], routes['departure']])[0]
    if entry[0] != home:
        raise ValueError('进场起点与选定出发点不一致')
    result = []
    scans = assignment['task'].get('waypoints_wgs84') if frame == 'WGS84' else assignment['task']['waypoints_m']
    if not isinstance(scans, list) or len(scans) != len(returns) or not returns:
        raise ValueError('进返场航线缺少对应的侦察航点')
    for i, item in enumerate(returns):
        if not isinstance(item, dict) or type(item.get('waypoint_index')) is not int or item['waypoint_index'] != i + 1:
            raise ValueError('返航路线航点编号必须从 1 连续编号')
        values = path(item['path'])
        expected = [scans[i][1], scans[i][0]] if frame == 'WGS84' else scans[i][:2]
        tolerance = 2e-8 if frame == 'WGS84' else 1e-4
        if math.dist(values[0][:2], expected) > tolerance or values[-1] != home:
            raise ValueError('返航路线起终点与侦察航点或出发点不一致')
        result.append(dict(waypoint_index=i + 1, path=values))
    if entry[-1] != result[0]['path'][0]:
        raise ValueError('进场终点必须是第一个侦察航点')
    common = dict(schema_version=1, mission_id=assignment['mission_id'], uav_id=assignment['uav_id'],
                  assignment_checksum=assignment['assignment_checksum'], coordinate_frame=frame,
                  coordinate_order='longitude_latitude_relative_altitude' if frame == 'WGS84' else 'x_y_z',
                  altitude_frame='RELATIVE_TO_TAKEOFF', plan_sha256=routes['plan_sha256'])
    return dict(common, path=entry), dict(common, routes=result)
