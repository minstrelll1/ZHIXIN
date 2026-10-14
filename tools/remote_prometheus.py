"""SSH 单机准备：读取状态，未解锁时调用厂商 GPS 服务。无飞行/解锁接口。"""
import contextlib
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time
import xmlrpc.client


def current_boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def read_record(path, boot):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if data.get('boot_id') == boot else {}
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
        event.set()
    sub = rospy.Subscriber('/uav%d/prometheus/state' % uid, UAVState, receive, queue_size=1)
    try:
        event.wait(2.0)  # 单调实时时间；ROS 时钟异常不会造成无限等待。
    finally:
        sub.unregister()
    return result


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
    code, _, state = master.getSystemState('/competition_connection')
    if code != 1:
        raise RuntimeError('ROS Master 尚未就绪')
    nodes = {node for group in state for _, names in group for node in names}
    bridge = param('/communication_bridge', {}) if '/communication_bridge' in nodes else {}
    bridge = {key: bridge.get(key) for key in ('ROBOT_ID', 'uav_id', 'is_simulation', 'autoload',
               'uav_control_start', 'tcp_port', 'tcp_heartbeat_port', 'ground_station_ip')} if bridge else {}
    registered = any(name == '/uav%d/prometheus/state' % uid and '/communication_bridge' in names
                     for name, names in state[1])
    base_present = '/uav%d/mavros' % uid in nodes or '/uav_control_main_%d' % uid in nodes
    business_present = any(name in nodes for name in ('/su17_competition_executor',
        '/uav_yolo26_botsort_geolocation', '/servo_serial_node',
        '/competition_image_stamp_adapter', '/su17_onboard_image_sender',
        '/uav%d/target_geolocator' % uid, '/uav%d/gimbal_tracker_gx40_vertical' % uid,
        '/uav%d/target_maneuver_gx40_position_pid' % uid,
        '/uav%d/target_scheduler_gx40_position_pid' % uid,
        '/uav%d/trajectory_follower' % uid))
    rospy = ros_setup()
    flight = flight_sample(rospy, uid) if base_present else {}
    result = dict(boot_id=boot, bridge=bridge, registered=registered, base_present=base_present,
                  business_present=business_present, flight=flight, prepared=bool(record.get('ready')))
    if payload['action'] == 'prometheus_probe':
        return result
    if payload['action'] != 'prometheus_gps' or payload.get('expected_boot_id') != boot:
        raise RuntimeError('准备操作或机载开机标识不匹配，未切换定位源')
    import fcntl
    with (root / 'prometheus_connection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = read_record(record_path, boot)
        if record.get('ready'):
            return dict(ready=True, detail='本次开机 GPS 准备已完成，未重复切换')
        if not flight.get('connected') or flight.get('armed') or business_present:
            return dict(ready=False, detail='等待未解锁的有效飞控状态；已有业务运行时不自动切换定位源')
        preset = param('/switch_location_source/Location_Source/GPS', {})
        if preset.get('control', {}).get('location_source') != 4:
            raise RuntimeError('厂商 GPS 定位源预设缺失，未改变参数')
        if not record.get('requested'):
            from prometheus_msgs.srv import SwitchLocationSource
            name = '/uav%d/prometheus/switch_location_source' % uid
            rospy.wait_for_service(name, timeout=2)
            # 先写意图；SSH 回包丢失也不能在重连时再次切换。
            record = dict(boot_id=boot, requested=True, at=time.time())
            save_record(record_path, record)
            reply = rospy.ServiceProxy(name, SwitchLocationSource)(4)
            record['accepted'] = bool(reply.success)
            record['message'] = reply.message
            save_record(record_path, record)
            if not reply.success:
                return dict(ready=False, detail='厂商拒绝 GPS 切换：' + reply.message)
            return dict(ready=False, detail='已请求厂商切换 GPS，等待实际定位源与飞控参数核验')
        # 不以服务回执代替实际状态；逐项只读查询 MAVROS 的飞控参数缓存。
        from mavros_msgs.srv import ParamGet
        missing = []
        for name, expected in preset.get('px4_params', {}).items():
            answer = rospy.ServiceProxy('/uav%d/mavros/param/get' % uid, ParamGet)(name)
            actual = answer.value.integer if isinstance(expected, int) else answer.value.real
            if not answer.success or abs(float(actual) - float(expected)) > 1e-5:
                missing.append(name)
        ready = flight.get('location_source') == 4 and not missing
        if ready:
            record.update(ready=True, verified_at=time.time())
            save_record(record_path, record)
        return dict(ready=ready, detail='厂商连接及 GPS 飞控参数核验通过' if ready else
                    '等待 GPS 定位源及飞控参数生效：' + ', '.join(missing))


def rpc(payload):
    # rospy 初始化或服务诊断不能污染 RPC JSON。
    with contextlib.redirect_stdout(io.StringIO()):
        result = run(payload)
    print(json.dumps(result, ensure_ascii=False))
