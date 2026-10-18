"""科目二双层任务的数据校验；不生成规划、不发出飞行控制指令。"""
import math


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(label + '必须为有限数值')
    return float(value)


def _points(values, label, maximum, gps=False):
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ValueError(label + '数量无效')
    for point in values:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError(label + '必须为二维坐标')
        x, y = (_number(v, label) for v in point)
        if gps and (abs(x) > 90 or abs(y) > 180):
            raise ValueError(label + '经纬度越界')


def validate_layers(assignment, maximum_waypoints=10000):
    """返回已校验的上下层，旧单层任务返回 None。原字段始终代表上层。"""
    task = assignment['task']
    if 'layers' not in task:
        return None
    if assignment.get('subject') != 'subject2' or assignment.get('controller_mode') != 'external':
        raise ValueError('双层任务仅支持科目二外部程序 B 模式')
    layers = task['layers']
    if (not isinstance(layers, list) or len(layers) != 2
            or any(not isinstance(layer, dict) for layer in layers)
            or [layer.get('layer_id') for layer in layers] != ['upper', 'lower']
            or task.get('layer_order') != ['upper', 'lower']):
        raise ValueError('双层任务必须按 upper、lower 顺序提供两层和 layer_order')
    if task.get('execution_mode') != 'upper_once_then_lower_once':
        raise ValueError('双层任务必须上层一趟、下层一趟')
    frame = assignment.get('coordinate_frame', 'WGS84')
    if frame not in ('WGS84', 'ENU'):
        raise ValueError('双层任务外部坐标系无效')
    total = 0
    for layer in layers:
        label = layer['layer_id']
        if _number(layer.get('altitude_m'), label + '高度') <= 0:
            raise ValueError('双层高度必须为正数')
        _points(layer.get('waypoints_m'), label + '平面航点', maximum_waypoints)
        total += len(layer['waypoints_m'])
        if frame == 'WGS84' or task.get('coordinate_frame') == 'LOCAL_NORTH_WEST':
            _points(layer.get('waypoints_wgs84'), label + 'GPS航点', maximum_waypoints, gps=True)
            if len(layer['waypoints_wgs84']) != len(layer['waypoints_m']):
                raise ValueError('每层 GPS 和平面航点必须逐一对应')
        if type(layer.get('scan_passes')) is not int or layer['scan_passes'] != 1:
            raise ValueError('双层任务每层只扫描一趟')
        if _number(layer.get('hover_scan_seconds'), label + '扫描时长') <= 0:
            raise ValueError('每层扫描时长必须为正数')
    if total > maximum_waypoints:
        raise ValueError('双层任务总航点数过多')
    upper, lower = layers
    if lower['altitude_m'] >= upper['altitude_m']:
        raise ValueError('下层高度必须低于上层高度')
    if not math.isclose(_number(assignment['target_altitude_m'], '起飞高度'), upper['altitude_m'], abs_tol=1e-6, rel_tol=0):
        raise ValueError('任务起飞高度必须等于上层高度，请使用匹配的高度方案')
    if task.get('waypoints_m') != upper['waypoints_m']:
        raise ValueError('task.waypoints_m 必须仅包含上层航点且顺序一致')
    if frame == 'WGS84':
        gps = task.get('waypoints_wgs84')
        if (not isinstance(gps, list) or any(not isinstance(p, (list, tuple)) for p in gps)
                or [p[:2] for p in gps] != upper['waypoints_wgs84']):
            raise ValueError('task.waypoints_wgs84 必须仅包含上层航点且顺序一致')
        if task.get('waypoints_gps') is not None:
            raise ValueError('双层任务不得同时提供旧 waypoints_gps 别名')
    routes = task.get('transit_routes')
    if not isinstance(routes, dict) or routes.get('schema_version') != 2:
        raise ValueError('双层任务必须提供六机上层航点的版本 2 返航路线')
    if routes.get('waypoint_layer', 'upper') != 'upper':
        raise ValueError('双层返航路线只能对应上层航点')
    return layers


def validate_layer_fleet(assignments):
    """分派任意一架之前，校验六机分层数据及返航目录是否确为上层。"""
    for assignment in assignments.values():
        validate_layers(assignment)
    if not any('layers' in a['task'] for a in assignments.values()):
        return
    frames = {a.get('coordinate_frame', 'WGS84') for a in assignments.values()}
    if len(frames) != 1 or set(assignments) != set(range(1, 7)):
        raise ValueError('双层返航目录需要六机使用同一坐标系')
    frame = next(iter(frames))
    catalog = {}
    for uid, assignment in assignments.items():
        task = assignment['task']
        catalog[str(uid)] = ([[p[1], p[0]] for p in task['waypoints_wgs84']]
                             if frame == 'WGS84' else task['waypoints_m'])
    for uid, assignment in assignments.items():
        routes = assignment['task'].get('transit_routes', {})
        if (routes.get('schema_version') != 2 or routes.get('recipient_uav_id') != uid
                or routes.get('route_scope') != 'all_uav_waypoints'
                or routes.get('coordinate_frame') != frame or routes.get('waypoints_by_uav') != catalog):
            raise ValueError('UAV{} 返航目录必须逐一对应六机上层航点，不得混入下层航点'.format(uid))
