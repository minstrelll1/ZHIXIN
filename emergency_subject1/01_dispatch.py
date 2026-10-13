#!/usr/bin/env python3
"""脚本一：默认保留原后端，在发布端 dispatch 下发六机固定任务。"""
import argparse
import copy
import os
import time
import uuid
from pathlib import Path

from common import (IDS, ROOT, Failure, RunLock, build_bundle, error_text, fleet_status,
                    load_fixed, load_settings, main_guard, parallel, peer_token,
                    read_json, request, require, require_bundle_on_fleet, save_json,
                    show_status, urls, validate_bundle)


def dispatch(config, endpoints, token, state_dir, *, elapsed_seconds=None,
             resume=False, new_mission=False, timeout=40):
    state_dir = Path(state_dir)
    path = state_dir / 'mission.json'
    with RunLock(state_dir):
        states = fleet_status(endpoints, token)
        for uid, s in states.items():
            t = s.get('telemetry') or {}
            require(s['fresh'] and s['connected'] and not t.get('armed'),
                    'UAV{} 未连接、遥测过期或已解锁；未开始分派。'.format(uid))
            require(t.get('task_phase') in ('', 'idle', 'assigned', 'takeoff_failed', 'landed', 'completed'),
                    'UAV{} 当前正在执行任务/已被接管；未开始分派。'.format(uid))
            require(all(s['control_settings'][k] == config[k] for k in
                        ('duration_seconds', 'return_battery_threshold', 'preflight_battery_min')),
                    'UAV{} 的配置与发布端不一致。'.format(uid))
        if resume:
            require(path.exists(), '没有可恢复的 run/mission.json。')
            require(elapsed_seconds is None, '--resume 继承本任务计时，不能重新设置 --elapsed-seconds。')
            bundle = validate_bundle(read_json(path))
            require(bundle['assignments']['1']['task']['recognition_selection']['category_ids']
                    == sorted(config['recognition_category_ids']), '识别类别已修改；请使用 --new-mission 创建新任务。')
            require(all(bundle[k] == config[k] for k in ('duration_seconds', 'return_battery_threshold')),
                    '任务期限/返航阈值已修改；请创建新任务。')
            require(all(not s['takeoff'] and not s['observed_started'] for s in states.values()),
                    '已经发送过起飞，不能重发任务。')
            clocks = [s['competition_time']['elapsed_seconds'] for s in states.values()
                      if s['mission_id'] == bundle['mission_id'] and s['competition_time'].get('running')]
            elapsed = max(clocks) if clocks else bundle['competition_time']['elapsed_seconds'] + max(0., time.time()-bundle['created_at'])
        else:
            require(not path.exists() or new_mission,
                    '已有任务包。补发使用 --resume；确需重新分派时使用 --new-mission。')
            require(elapsed_seconds is not None, '新任务必须用 --elapsed-seconds 指定当前比赛已用秒数。')
            bundle = build_bundle(config, 'subject1-emergency-' + uuid.uuid4().hex[:12],
                                  uuid.uuid4().hex, elapsed_seconds)
            elapsed = elapsed_seconds
            if path.exists():
                old = read_json(path)
                save_json(state_dir / ('archive-{}.json'.format(old['mission_id'])), old)
            save_json(path, bundle)  # All six validated and frozen before any transmission.
        anchor = time.monotonic()
        print('任务 {}，识别类别 {}，比赛已用 {:.1f} 秒。'.format(bundle['mission_id'],
            bundle['assignments']['1']['task']['recognition_selection']['category_ids'], elapsed), flush=True)
        started, next_send, attempts, last_errors = time.monotonic(), 0., {}, {}
        while time.monotonic()-started < timeout:
            states = fleet_status(endpoints, token)
            missing = [uid for uid, s in states.items() if not (
                s['fresh'] and s['connected'] and s['assignment_acked'] and not s['boot_changed']
                and s['assignment_checksum'] == bundle['assignments'][str(uid)]['assignment_checksum'])]
            if not missing:
                require_bundle_on_fleet(bundle, states)
                save_json(state_dir / 'dispatch_receipt.json', states)
                show_status(states)
                print('分派成功：6/6 机载完整任务回执一致。脚本一没有发送起飞。', flush=True)
                return bundle
            if time.monotonic() >= next_send:
                selected = [uid for uid in missing if attempts.get(uid, 0) < 3]
                def send(uid):
                    return request(endpoints[uid], token, '/assignment', dict(
                        assignment=bundle['assignments'][str(uid)],
                        elapsed_seconds=elapsed+time.monotonic()-anchor))
                _sent, errors = parallel(selected, send)
                last_errors.update(errors)
                for uid in selected:
                    attempts[uid] = attempts.get(uid, 0)+1
                    if uid not in errors:
                        last_errors.pop(uid, None)
                print('等待回执：{}；发送次数 {}。'.format(missing, attempts), flush=True)
                next_send = time.monotonic()+3
            time.sleep(.5)
        save_json(state_dir / 'dispatch_receipt.json', states)
        show_status(states)
        raise Failure('未收到全部六机回执。修复提示的问题后运行 dispatch --resume。\n' + error_text(last_errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config.json')
    parser.add_argument('--fleet', type=Path, default=ROOT/'fleet.json')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--mode', choices=('backend', 'standalone'),
                        help='backend 保留原后端；standalone 完全接管（不含图片与上报）')
    sub = parser.add_subparsers(dest='action', required=True)
    service = sub.add_parser('serve', help='在每台地面电脑运行，持续保留这个窗口')
    service.add_argument('--uav', type=int, choices=IDS, required=True)
    service.add_argument('--bind', default='0.0.0.0')
    send = sub.add_parser('dispatch', help='仅在发布端运行：发送六机固定任务，不起飞')
    send.add_argument('--elapsed-seconds', type=float)
    group = send.add_mutually_exclusive_group()
    group.add_argument('--resume', action='store_true')
    group.add_argument('--new-mission', action='store_true')
    send.add_argument('--timeout', type=float, default=60)
    sub.add_parser('status', help='只读查询六机回执及飞行阶段')
    sub.add_parser('inspect', help='离线检查固定任务、识别类别和机位，不连接飞机')
    args = parser.parse_args()
    config, fleet = load_settings(args.config, args.fleet)
    mode = args.mode or config.get('mode', 'backend')
    require(mode in ('backend', 'standalone'), 'config.json 的 mode 必须为 backend 或 standalone。')
    args.state_dir = args.state_dir or (ROOT/'run'/'backend' if mode == 'backend' else ROOT/'run')
    if args.action == 'inspect':
        bundle = build_bundle(config, 'offline-inspection', 'offline-inspection', 0)
        for pos in load_fixed()['layout']['positions']:
            uid = pos['uav_id']
            task = bundle['assignments'][str(uid)]['task']
            print('UAV{} 高度={}m 纬度={:.12f} 经度={:.12f} 扫描点={} 返航路线={}'.format(
                uid, pos['altitude_m'], pos['latitude'], pos['longitude'],
                len(task['waypoints_m']), len(task['transit_routes']['return_paths'])))
        print('类别：{}；起飞后任务期限：{} 秒。'.format(config['recognition_category_ids'], config['duration_seconds']))
        return
    if mode == 'backend':
        require(args.action != 'serve', '共存模式保留原后端，不需要 serve；请在发布端直接运行 dispatch。')
        import backend_client
        if args.action == 'status':
            backend_client.show(backend_client.Backend(config.get('backend_url', 'http://127.0.0.1:8000')).call('/status'))
        else:
            require(args.timeout > 0, '--timeout 必须大于 0。')
            backend_client.dispatch(config, args.state_dir, elapsed_seconds=args.elapsed_seconds,
                                    resume=args.resume, new_mission=args.new_mission, timeout=args.timeout)
        return
    token = peer_token()
    if args.action == 'serve':
        from relay import Relay, make_http_server
        tcp_token = os.environ.get('COMPETITION_TCP_TOKEN') or os.environ.get('AUTH_TOKEN', '')
        require(bool(tcp_token), '请先加载原 local_tokens.ps1（AUTH_TOKEN 或 COMPETITION_TCP_TOKEN）。')
        relay = Relay(args.uav, config, fleet, token, tcp_token, args.state_dir, args.bind)
        server = make_http_server(relay, args.bind, config['relay_port'])
        try:
            relay.start()
            print('备用节点 UAV{}：任务 TCP {}，控制 HTTP {}。等待机载自动重连；不会自动起飞。'.format(
                args.uav, relay.adapter.bound_port, server.server_address[1]), flush=True)
            server.serve_forever(poll_interval=.3)
        finally:
            server.server_close()
            relay.stop()
    elif args.action == 'status':
        show_status(fleet_status(urls(config, fleet), token))
    else:
        require(args.timeout > 0, '--timeout 必须大于 0。')
        dispatch(config, urls(config, fleet), token, args.state_dir,
                 elapsed_seconds=args.elapsed_seconds, resume=args.resume,
                 new_mission=args.new_mission, timeout=args.timeout)


if __name__ == '__main__':
    raise SystemExit(main_guard(main))
