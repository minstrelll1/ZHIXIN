#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from competition_shared.fleet import FleetStore
from competition_shared.runtime import ground_environment

if __name__ == '__main__':
    p=argparse.ArgumentParser(description='读取统一机队配置')
    p.add_argument('--fleet', required=True)
    p.add_argument('--terminal', required=True, type=int)
    args=p.parse_args()
    try:
        local, env=ground_environment(FleetStore(args.fleet).read(), args.terminal)
        print(json.dumps({'local':local, 'environment':env},ensure_ascii=False))
    except Exception as error:
        print('机队配置无效：'+str(error),file=sys.stderr)
        sys.exit(1)
