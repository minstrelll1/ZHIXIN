#!/usr/bin/env python3
"""只读核验飞控身份并启动竞赛节点，不启动或修改厂商程序。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from competition_shared.fleet import FleetStore, resolved_vehicle, revision, vehicle

STATE_MD5 = {'p600': '61e95e9b6634e22769af5718eecfc53c', 'su17': '19d12342d45fd9dbd79a3c5ff86556ac'}


def check_identity(model, expected_id=None):
    import rospy
    from mavros_msgs.srv import ParamGet
    from prometheus_msgs.msg import UAVState, UAVControlState
    if UAVState._md5sum != STATE_MD5[model]:
        raise ValueError('当前 UAVState 消息与所选机型不一致，请检查 ROS 环境覆盖顺序')
    rospy.init_node('competition_identity_check', anonymous=True, disable_signals=True)
    candidates = sorted({int(m.group(1)) for name, _ in rospy.get_published_topics()
                         for m in [re.fullmatch(r'/uav([1-6])/prometheus/state', name)] if m})
    if len(candidates) != 1:
        raise ValueError('必须发现且仅发现一个本机 /uav1～6/prometheus/state 话题，当前：%s' % candidates)
    uid = candidates[0]
    service = '/uav%d/mavros/param/get' % uid
    rospy.wait_for_service(service, timeout=8)
    response = rospy.ServiceProxy(service, ParamGet)(param_id='MAV_SYS_ID')
    if not response.success:
        raise ValueError('无法从 MAVROS 读取飞控 MAV_SYS_ID；先确认飞控已连接且参数已加载')
    actual = int(response.value.integer)
    if actual != uid or (expected_id is not None and actual != expected_id):
        raise ValueError('飞控 MAV_SYS_ID=%s 与话题编号 %s 或期望编号 %s 不一致' % (actual, uid, expected_id))
    state = rospy.wait_for_message('/uav%d/prometheus/state' % uid, UAVState, timeout=8)
    control = rospy.wait_for_message('/uav%d/prometheus/control_state' % uid, UAVControlState, timeout=8)
    if int(state.uav_id) != uid or not state.connected:
        raise ValueError('UAVState 编号不匹配或飞控未连接')
    if hasattr(control, 'uav_id') and int(control.uav_id) != uid:
        raise ValueError('控制状态编号与飞控编号不一致')
    machine = Path('/etc/machine-id').read_text().strip()
    if not machine:
        raise ValueError('无法读取机载计算机的稳定设备标识')
    return {'uav_id': uid, 'flight_controller_id': actual, 'ros_namespace': '/uav%d' % uid,
            'model': model, 'device_id': hashlib.sha256(machine.encode()).hexdigest()[:24],
            'identity_verified': True, 'software_version': '0.2.0',
            'capabilities': {'protocol': 1, 'tasks': True, 'image_return': True, 'pointcloud': True, 'external_mission': True}}


def fixed_binding_identity(model, expected_id=None):
    """根据已经固化的机队绑定生成身份，不访问 ROS/MAVROS。"""
    if expected_id is None:
        raise ValueError('--direct 模式必须同时提供 --expect-uav-id')
    machine = Path('/etc/machine-id').read_text().strip()
    if not machine:
        raise ValueError('无法读取机载计算机的稳定设备标识')
    return {'uav_id': expected_id, 'flight_controller_id': expected_id,
            'ros_namespace': '/uav%d' % expected_id, 'model': model,
            'device_id': hashlib.sha256(machine.encode()).hexdigest()[:24],
            'identity_verified': True, 'identity_source': 'fixed_fleet_binding',
            'software_version': '0.2.0',
            'capabilities': {'protocol': 1, 'tasks': True, 'image_return': True,
                             'pointcloud': True, 'external_mission': True}}


def main():
    parser = argparse.ArgumentParser(description='P600 / SU17 机载启动与只读检查')
    parser.add_argument('--model', choices=sorted(STATE_MD5), default='p600')
    parser.add_argument('--fleet', default=str(Path(__file__).resolve().parents[1] / 'config/fleet.json'))
    parser.add_argument('--expect-uav-id', type=int, choices=range(1, 7))
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--direct', action='store_true',
                        help='按固定机队绑定直接启动，不等待 Prometheus 状态话题')
    parser.add_argument('--enable-motion', action='store_true')
    args = parser.parse_args()
    identity = fixed_binding_identity(args.model, args.expect_uav_id) if args.direct else check_identity(args.model, args.expect_uav_id)
    config = FleetStore(args.fleet).read()
    v = resolved_vehicle(vehicle(config, identity['uav_id']))
    if args.direct:
        print('固定机队绑定通过（未访问 ROS/MAVROS）：%s / UAV%s / %s' % (args.model.upper(), identity['uav_id'], identity['ros_namespace']))
    else:
        print('飞控身份核验通过：%s / UAV%s / %s' % (args.model.upper(), identity['uav_id'], identity['ros_namespace']))
    print('设备标识：' + identity['device_id'])
    print('算法与图像缓存应共用的话题：' + v['image_topic'])
    print('待配置：' + ('、'.join(v['pending']) or '无'))
    if args.check:
        print('检查完成，未启动飞行任务。')
        return
    if v['model'] != args.model or not v['enabled']:
        raise ValueError('飞控编号对应的配置机型不一致，或该飞机已停用')
    # IP、UAV 编号和机型已经由固定机队配置确定。device_id 仍随 hello 上报，
    # 但不再把它作为启动前的额外人工绑定步骤。
    if not v['ground_host']:
        raise ValueError('请填写 ground_host，程序不会按编号推测 IP')
    if not os.environ.get('AUTH_TOKEN', ''):
        raise ValueError('请设置 AUTH_TOKEN 环境变量或 tools/local_tokens.env')
    identity['fleet_revision'] = revision(config)
    os.environ['COMPETITION_ONBOARD_IDENTITY'] = json.dumps(identity, ensure_ascii=False)
    os.environ['COMPETITION_ONBOARD_VEHICLE'] = json.dumps(v, ensure_ascii=False)
    argv = ['roslaunch', 'su17_competition_executor', 'onboard_competition_stack.launch',
            'uav_id:=%s' % v['uav_id'], 'local_ros_uav_id:=%s' % v['uav_id'],
            'ground_host:=%s' % v['ground_host'], 'task_port:=%s' % v['task_port'],
            'image_port:=%s' % v['image_port'], 'image_topic:=%s' % v['image_topic'],
            'stamp_mode:=%s' % v['image_stamp_mode'], 'enable_stamp_adapter:=true',
            'enable_motion:=%s' % str(args.enable_motion).lower(),
            'max_distance_from_home_m:=%s' % v['max_distance_m']]
    print('正在启动 %s / UAV%s 的竞赛程序，飞行控制：%s' % (args.model.upper(), v['uav_id'], '开启' if args.enable_motion else '关闭'), flush=True)
    os.execvp(argv[0], argv)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('机载启动检查失败：%s' % error, file=sys.stderr)
        sys.exit(1)
