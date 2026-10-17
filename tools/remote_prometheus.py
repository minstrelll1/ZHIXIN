"""SSH 单机准备：优先只读复用 GPS；非 GPS 冷启动才允许一次切换。"""
import contextlib
import io
import json
import math
from pathlib import Path
import socket
import sys
import threading
import time
import xmlrpc.client


CONTROL_KEYS = ('location_source', 'pos_controller', 'maximum_safe_vel_xy',
                'maximum_safe_vel_z', 'maximum_vel_error_for_vision',
                'maximum_vel_error_for_Odom', 'Takeoff_height', 'Land_speed')
PX4_AUDIT_KEYS = ('EKF2_AID_MASK', 'EKF2_HGT_MODE', 'COM_ARM_WO_GPS',
                  'MC_YAWRATE_MAX', 'MPC_Z_VEL_MAX_UP', 'MPC_Z_VEL_MAX_DN',
                  'MPC_XY_VEL_MAX')
PX4_INTEGER_KEYS = {'EKF2_AID_MASK', 'EKF2_HGT_MODE', 'COM_ARM_WO_GPS'}


def parameter_snapshot(rospy, param, flight, uid, preset):
    """只读采样；保留采样失败，不能用预设值冒充实际值。"""
    from mavros_msgs.srv import ParamGet
    control = param('/uav_control_main_%d/control' % uid, {}) or {}
    fence = param('/uav_control_main_%d/geo_fence' % uid, {}) or {}
    def clean(value):
        return None if isinstance(value, float) and not math.isfinite(value) else value
    snapshot = dict(flight=dict(flight), control={key: clean(control.get(key)) for key in CONTROL_KEYS},
                    geo_fence={key: clean(fence.get(key)) for key in ('x_min','x_max','y_min','y_max','z_min','z_max')},
                    px4_params={}, read_errors={})
    get = rospy.ServiceProxy('/uav%d/mavros/param/get' % uid, ParamGet)
    expected = preset.get('px4_params', {})
    for name in sorted(set(PX4_AUDIT_KEYS) | set(expected)):
        try:
            answer = get(name)
            if not answer.success:
                raise RuntimeError('MAVROS 参数缓存未返回有效值')
            integer = name in PX4_INTEGER_KEYS or type(expected.get(name)) is int
            value = answer.value.integer if integer else answer.value.real
            if not math.isfinite(float(value)):
                raise ValueError('参数不是有限数值')
            snapshot['px4_params'][name] = value
        except Exception as error:
            snapshot['read_errors'][name] = str(error)
    return snapshot


def gps_verification(snapshot, preset, switched=False):
    problems = []
    control, actual = snapshot['control'], snapshot['px4_params']
    if snapshot['flight'].get('location_source') != 4 or control.get('location_source') != 4:
        problems.append('GPS 定位源尚未一致')
    for name in ('maximum_safe_vel_xy', 'maximum_safe_vel_z'):
        value = control.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            problems.append(name + ' 缺失或无效')
    expected = preset.get('px4_params', {})
    # 复用仅核验定位相关设置，不要求用户调好的速度/围栏/解锁条件/偏航参数等于预设。
    aid = actual.get('EKF2_AID_MASK')
    if type(aid) is not int or not aid & 1:
        problems.append('EKF2_AID_MASK 未启用 GPS 融合或读取失败')
    height = expected.get('EKF2_HGT_MODE', 1)
    if actual.get('EKF2_HGT_MODE') != height:
        problems.append('EKF2_HGT_MODE 不符合 GPS 高度预设或读取失败')
    if switched:
        for name, value in expected.items():
            if name not in actual or abs(float(actual[name]) - float(value)) > 1e-5:
                problems.append(name + ' 尚未符合切换预设')
    return list(dict.fromkeys(problems))


def parameter_diff(before, after):
    """仅比较实际采样的参数；缺少准备前快照时不编造变化。"""
    if before is None or after is None:
        return None
    changes = {}
    for group in ('control', 'geo_fence', 'px4_params'):
        left, right = before.get(group, {}), after.get(group, {})
        for name in sorted(set(left) | set(right)):
            if left.get(name) != right.get(name):
                changes[group + '/' + name] = dict(before=left.get(name), after=right.get(name))
    return changes


def preparation_result(record_path, record, snapshot, ready, detail):
    audit = dict(boot_id=record['boot_id'], mode=record.get('mode', 'switch_to_gps'),
                 requested=bool(record.get('requested')), accepted=record.get('accepted'),
                 before=record.get('before'), after=snapshot,
                 parameter_changes=parameter_diff(record.get('before'), snapshot),
                 ready=ready, detail=detail)
    previous = record.get('audit', {})
    # 失败轮询不重复刷日志；地面重开可从记录补取最终证据。
    if any(previous.get(key) != value for key, value in audit.items()):
        audit.update(recorded_at=time.time(), recorded_monotonic=time.monotonic())
        record.update(audit=audit, ready=ready)
        save_record(record_path, record)
        with record_path.with_name('prometheus_connection_audit.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps(audit, ensure_ascii=False, allow_nan=False) + '\n')
    else:
        audit = previous
    return dict(ready=ready, detail=detail, preparation_audit=audit)


def current_boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def read_record(path, boot):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) and data.get('boot_id') == boot else {}
    except (OSError, ValueError):
        return {}


def save_record(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    temp.replace(path)


def ros_setup():
    # 独立 SSH 环境也能读取 ROS 消息，不依赖用户 .bashrc 的交互分支。
    for path in [Path('/opt/ros/noetic/lib/python3/dist-packages'),
                 Path.home() / 'prometheus_mavros/devel/lib/python3/dist-packages',
                 Path.home() / 'p600_experiment/devel/lib/python3/dist-packages',
                 Path.home() / 'su17_experiment/devel/lib/python3/dist-packages']:
        if path.is_dir():
            sys.path.insert(0, str(path))
    import rospy
    rospy.init_node('competition_connection_probe', anonymous=True, disable_signals=True)
    return rospy


def flight_sample(rospy, uid):
    from prometheus_msgs.msg import UAVState
    event, result = threading.Event(), {}
    def receive(msg):
        result.update(connected=bool(msg.connected), armed=bool(msg.armed),
                      location_source=int(msg.location_source), mode=msg.mode)
        # 仅采集现有消息；不增加等待，不用诊断字段控制准备或飞行。
        try:
            for name in ('odom_valid', 'gps_status', 'gps_num', 'latitude', 'longitude', 'altitude', 'rel_alt'):
                value = getattr(msg, name, None)
                result[name] = None if isinstance(value, float) and not math.isfinite(value) else value
            result['velocity'] = list(msg.velocity)
            result['source_stamp'] = msg.header.stamp.to_sec()
        except (AttributeError, TypeError, ValueError):
            result['diagnostic_detail'] = '部分旧版状态字段不可用'
        result['sampled_wall_time'] = time.time()
        result['sampled_monotonic'] = time.monotonic()
        event.set()
    sub = rospy.Subscriber('/uav%d/prometheus/state' % uid, UAVState, receive, queue_size=1)
    try:
        event.wait(2.0)  # 单调实时时间；ROS 时钟异常不会造成无限等待。
    finally:
        sub.unregister()
    return result


def has_business(nodes, uid):
    return any(name in nodes for name in ('/su17_competition_executor',
        '/uav_yolo26_botsort_geolocation', '/uav_yolo26_vb_geolocation',
        '/uav_yolo26_ren_geolocation', '/servo_serial_node',
        '/competition_image_stamp_adapter', '/su17_onboard_image_sender',
        '/uav%d/target_geolocator' % uid, '/uav%d/gimbal_tracker_gx40_vertical' % uid,
        '/uav%d/target_maneuver_gx40_position_pid' % uid,
        '/uav%d/target_scheduler_gx40_position_pid' % uid,
        '/uav%d/trajectory_follower' % uid))


def run(payload):
    uid = int(payload['uav_id'])
    if uid not in range(1, 7):
        raise ValueError('无人机编号错误')
    boot = current_boot_id()
    root = Path.home() / 'competition_development/ground_runtime' / ('programs_uav%d' % uid)
    root.mkdir(parents=True, exist_ok=True)
    record_path = root / 'prometheus_connection.json'
    record = read_record(record_path, boot)
    socket.setdefaulttimeout(2)
    master = xmlrpc.client.ServerProxy('http://127.0.0.1:11311')
    def param(name, default=None):
        code, _, value = master.getParam('/competition_connection', name)
        return value if code == 1 else default
    try:
        code, _, state = master.getSystemState('/competition_connection')
    except (OSError, xmlrpc.client.Error) as error:
        raise RuntimeError('[ros_master] SSH已执行远端代码，但机载ROS Master 127.0.0.1:11311不可用；boot_id=%s；%s' % (boot, error)) from error
    if code != 1:
        raise RuntimeError('[ros_master] ROS Master 尚未就绪；boot_id=' + boot)
    nodes = {node for group in state for _, names in group for node in names}
    bridge = param('/communication_bridge', {}) if '/communication_bridge' in nodes else {}
    bridge = {key: bridge.get(key) for key in ('ROBOT_ID', 'uav_id', 'is_simulation', 'autoload',
               'uav_control_start', 'tcp_port', 'tcp_heartbeat_port', 'ground_station_ip')} if bridge else {}
    registered = any(name == '/uav%d/prometheus/state' % uid and '/communication_bridge' in names
                     for name, names in state[1])
    base_present = '/uav%d/mavros' % uid in nodes or '/uav_control_main_%d' % uid in nodes
    business_present = has_business(nodes, uid)
    rospy = ros_setup()
    flight = flight_sample(rospy, uid) if base_present else {}
    result = dict(boot_id=boot, bridge=bridge, registered=registered, base_present=base_present,
                  business_present=business_present, flight=flight, prepared=bool(record.get('ready')),
                  preparation_audit=record.get('audit'))
    if payload['action'] == 'prometheus_probe':
        return result
    if payload['action'] != 'prometheus_gps' or payload.get('expected_boot_id') != boot:
        raise RuntimeError('准备操作或机载开机标识不匹配，未切换定位源')
    import fcntl
    with (root / 'prometheus_connection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = read_record(record_path, boot)
        if record.get('ready'):
            return dict(ready=True, detail='本次开机 GPS 准备已完成，未重复切换',
                        preparation_audit=record.get('audit'))
        if not flight.get('connected') or flight.get('armed') or business_present:
            return dict(ready=False, detail='等待未解锁的有效飞控状态；已有业务运行时不自动切换定位源')
        # 首次观察到 GPS 就锁定只读复用；即使核验失败、丢包或地面重开，也不能退回整套预设重置。
        if not record.get('mode'):
            mode = 'switch_to_gps' if record.get('requested') else (
                'reuse_existing_gps' if flight.get('location_source') == 4 else '')
            if mode:
                record.update(boot_id=boot, mode=mode)
                save_record(record_path, record)
        preset = param('/switch_location_source/Location_Source/GPS', {})
        if record.get('mode') == 'reuse_existing_gps':
            before = parameter_snapshot(rospy, param, flight, uid, preset)
            if 'before' not in record:
                record['before'] = before
                save_record(record_path, record)
            after = parameter_snapshot(rospy, param, flight, uid, preset)
            problems = gps_verification(after, preset)
            detail = ('已有 GPS 配置核验通过，已复用；未调用定位源切换服务' if not problems else
                      '已有 GPS 配置核验未通过：' + '；'.join(problems) + '；保留现有参数，不自动重新切换 GPS')
            return preparation_result(record_path, record, after, not problems, detail)
        if not record.get('requested'):
            if type(flight.get('location_source')) is not int or flight['location_source'] not in range(12):
                return dict(ready=False, detail='等待明确的当前定位源；未调用 GPS 切换')
            if preset.get('control', {}).get('location_source') != 4:
                raise RuntimeError('厂商 GPS 定位源预设缺失，未改变参数')
            from prometheus_msgs.srv import SwitchLocationSource
            name = '/uav%d/prometheus/switch_location_source' % uid
            rospy.wait_for_service(name, timeout=2)
            before = parameter_snapshot(rospy, param, flight, uid, preset)
            # 参数采样期间可能已经解锁、切换定位源或启动业务，发送前再次核对。
            fresh_flight = flight_sample(rospy, uid)
            code, _, fresh_state = master.getSystemState('/competition_connection')
            fresh_nodes = {node for group in fresh_state for _, names in group for node in names} if code == 1 else set()
            if (code != 1 or not fresh_flight.get('connected') or fresh_flight.get('armed')
                    or has_business(fresh_nodes, uid)):
                return dict(ready=False, detail='准备期间飞控或业务状态变化，未切换 GPS')
            if fresh_flight.get('location_source') == 4 or before['control'].get('location_source') == 4:
                record.update(boot_id=boot, mode='reuse_existing_gps', before=before)
                save_record(record_path, record)
                return dict(ready=False, detail='定位源已变为 GPS，下次只读核验并复用；未调用切换服务')
            if fresh_flight.get('location_source') != flight.get('location_source'):
                return dict(ready=False, detail='定位源正在变化，未切换 GPS')
            # 先写意图；SSH 回包丢失也不能在重连时再次切换。
            record = dict(boot_id=boot, mode='switch_to_gps', requested=True, at=time.time(), before=before)
            save_record(record_path, record)
            try:
                reply = rospy.ServiceProxy(name, SwitchLocationSource)(4)
            except Exception as error:
                return preparation_result(record_path, record, None, False,
                    'GPS 切换回执未确认：%s；后续只读核验，不重复发送' % error)
            record['accepted'] = bool(reply.success)
            record['message'] = reply.message
            save_record(record_path, record)
            after = parameter_snapshot(rospy, param, fresh_flight, uid, preset)
            if not reply.success:
                return preparation_result(record_path, record, after, False, '厂商拒绝 GPS 切换：' + reply.message)
            return preparation_result(record_path, record, after, False, '已请求厂商切换 GPS，等待实际定位源与飞控参数核验')
        after = parameter_snapshot(rospy, param, flight, uid, preset)
        missing = gps_verification(after, preset, switched=True)
        return preparation_result(record_path, record, after, not missing,
            '厂商连接及 GPS 参数核验通过' if not missing else '等待 GPS 定位源及飞控参数生效：' + '；'.join(missing))


def rpc(payload):
    # rospy 初始化或服务诊断不能污染 RPC JSON。
    with contextlib.redirect_stdout(io.StringIO()):
        result = run(payload)
    print(json.dumps(result, ensure_ascii=False))
