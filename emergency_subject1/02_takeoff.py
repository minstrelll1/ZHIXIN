#!/usr/bin/env python3
"""脚本二：默认保留原后端，在发布端核对六机并发送一键起飞；默认只检查。"""
import argparse
import time
from pathlib import Path

from common import (FAILED_PHASES, IDS, ROOT, Failure, RunLock, error_text, fleet_status,
                    load_settings, main_guard, parallel, peer_token, read_json,
                    request, require, require_bundle_on_fleet, save_json, show_status,
                    urls, validate_bundle)


def takeoff(endpoints, token, state_dir, *, send=False, retry_missing=False, timeout=30):
    state_dir = Path(state_dir)
    with RunLock(state_dir):
        bundle = validate_bundle(read_json(state_dir/'mission.json'))
        states = fleet_status(endpoints, token)
        require_bundle_on_fleet(bundle, states)
        failed = {uid: s.get('failure_phase') or s.get('telemetry', {}).get('task_phase')
                  for uid, s in states.items() if s.get('failure_phase')
                  or s.get('telemetry', {}).get('task_phase') in FAILED_PHASES}
        require(not failed, '存在起飞失败或遥控器接管，不能继续：\n' + error_text(failed))
        pending = [uid for uid, state in states.items() if not state['observed_started']]
        show_status(states)
        if not pending:
            print('六机均已观察到启动；没有再次发送起飞。', flush=True)
            return states
        if send and not retry_missing:
            require(not any(s['takeoff'] for s in states.values()),
                    '已有起飞发送记录。先检查 status；仅补发未启动飞机时加 --retry-missing。')
        command_id = 'takeoff-' + bundle['mission_id']
        def payload(uid):
            return dict(mission_id=bundle['mission_id'],
                        assignment_checksum=bundle['assignments'][str(uid)]['assignment_checksum'])
        prepared, errors = parallel(pending, lambda uid: request(
            endpoints[uid], token, '/preflight', payload(uid), timeout=18))
        require(not errors, '六机起飞检查未通过，本轮没有发送起飞：\n' + error_text(errors))
        if not send:
            print('待起飞飞机检查通过；当前只检查。实际起飞请添加 --send。', flush=True)
            return states
        # Check all six again after potentially slow SSH checks, before any takeoff write.
        states = fleet_status(endpoints, token)
        require_bundle_on_fleet(bundle, states)
        require(not any(s.get('failure_phase') or s.get('telemetry', {}).get('task_phase') in FAILED_PHASES
                        for s in states.values()), '预检期间发生起飞失败/遥控器接管，本轮不发送。')
        results, errors = parallel(pending, lambda uid: request(endpoints[uid], token, '/takeoff',
            dict(payload(uid), token=prepared[uid]['token'], command_id=command_id,
                 retry_missing=retry_missing)))
        save_json(state_dir/'takeoff_send_result.json', dict(mission_id=bundle['mission_id'], results=results, errors=errors))
        print('发送结果已保存；正在等待机载进入自动起飞阶段。', flush=True)
        started = time.monotonic()
        while time.monotonic()-started < timeout:
            states = fleet_status(endpoints, token)
            require_bundle_on_fleet(bundle, states)
            failures = {uid: s.get('failure_phase') or s.get('telemetry', {}).get('task_phase')
                        for uid, s in states.items() if s.get('failure_phase')
                        or s.get('telemetry', {}).get('task_phase') in FAILED_PHASES}
            if failures:
                save_json(state_dir/'takeoff_receipt.json', states)
                raise Failure('机载报告失败/接管，不会自动重启任务：\n' + error_text(failures))
            if all(s['observed_started'] for s in states.values()):
                save_json(state_dir/'takeoff_receipt.json', states)
                show_status(states)
                print('6/6 已观察到机载启动。这表示指令已执行到起飞流程；到高后由原机载程序自动发布航线并启动程序 B。', flush=True)
                return states
            time.sleep(.5)
        save_json(state_dir/'takeoff_receipt.json', states)
        show_status(states)
        missing = [uid for uid, s in states.items() if not s['observed_started']]
        raise Failure('尚未观察到 UAV{} 启动。检查状态后可 --send --retry-missing；已启动飞机不会重发。\n{}'.format(
            missing, error_text(errors)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config.json')
    parser.add_argument('--fleet', type=Path, default=ROOT/'fleet.json')
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--mode', choices=('backend', 'standalone'))
    parser.add_argument('--send', action='store_true', help='实际发送起飞；不加此参数只检查')
    parser.add_argument('--retry-missing', action='store_true', help='仅 standalone 模式：补发未启动的飞机，保留原任务期限')
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    require(args.timeout > 0, '--timeout 必须大于 0。')
    require(not args.retry_missing or args.send, '--retry-missing 必须与 --send 一起使用。')
    config, fleet = load_settings(args.config, args.fleet)
    mode = args.mode or config.get('mode', 'backend')
    require(mode in ('backend', 'standalone'), 'config.json 的 mode 必须为 backend 或 standalone。')
    args.state_dir = args.state_dir or (ROOT/'run'/'backend' if mode == 'backend' else ROOT/'run')
    if mode == 'backend':
        import backend_client
        backend_client.takeoff(config, args.state_dir, send=args.send,
                               retry_missing=args.retry_missing, timeout=args.timeout)
        return
    takeoff(urls(config, fleet), peer_token(), args.state_dir,
            send=args.send, retry_missing=args.retry_missing, timeout=args.timeout)


if __name__ == '__main__':
    raise SystemExit(main_guard(main))
