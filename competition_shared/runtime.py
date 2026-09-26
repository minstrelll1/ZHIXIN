"""将机队配置转换为现有服务参数；这里不推测网络地址。"""
from .fleet import resolved_vehicle, vehicle


def ground_environment(config, terminal_id):
    matches = [v for v in config['vehicles'] if v['ground_terminal_id'] == terminal_id and v['enabled']]
    if len(matches) != 1:
        raise ValueError('地面终端编号必须唯一对应一架启用的飞机')
    local = resolved_vehicle(matches[0])
    uid = local['uav_id']
    peers = [v for v in config['vehicles'] if v['enabled'] and v['uav_id'] != uid and v['peer_host']]
    host = local['onboard_host']
    env = {
        'COMPETITION_LOCAL_UAV_ID': str(uid),
        'COMPETITION_GROUND_NODE_ID': 'ground-%s' % terminal_id,
        'COMPETITION_GROUND_PEERS': ';'.join('%s=http://%s:%s' % (v['uav_id'], v['peer_host'], v['web_port']) for v in peers),
        'COMPETITION_TCP_PORT': str(local['task_port']),
        'COMPETITION_PORT': str(local['web_port']),
        'COMPETITION_UAV_SSH_HOSTS': '%s=%s' % (uid, host) if host else '',
        'COMPETITION_UAV_TRAFFIC_HOSTS': '%s=%s' % (uid, host) if host else '',
        'COMPETITION_VIDEO_RTSP_SOURCE': local['video_rtsp_source'],
        'COMPETITION_VIDEO_SOURCES': '%s=http://127.0.0.1:%s/uav%s/' % (uid, local['video_webrtc_port'], uid) if local['video_rtsp_source'] else '',
        'COMPETITION_POINTCLOUD_SOURCE': local['pointcloud_source'],
        'COMPETITION_POINTCLOUD_RELAY_UAV_IDS': str(uid),
        'COMPETITION_POINTCLOUD_TOPICS': '%s=%s' % (uid, local['pointcloud_topic']),
        'COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS': '%s=%s' % (uid, host) if host and local['pointcloud_enabled'] else '',
        'COMPETITION_POINTCLOUD_ROSBRIDGE_PORT': str(local['rosbridge_port']),
        'COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP': local['ground_host'] if local['pointcloud_enabled'] and host and local['pointcloud_source'] == 'groundstation_shared' else '',
        'COMPETITION_POINTCLOUD_CAPTURE_REMOTE_IP': host,
        'COMPETITION_POINTCLOUD_CAPTURE_REMOTE_PORT': str(local['rosbridge_port']),
        'COMPETITION_POINTCLOUD_CAPTURE_UAV_ID': str(uid),
    }
    return local, env
