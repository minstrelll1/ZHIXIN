"""已导入的决赛/许昌双层固定方案；运行时不求解分区和扫描航点。"""
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

PROFILES = ('final_scene_1', 'final_scene_2', 'xuchang_small_2')
NAMES = dict(zip(PROFILES, ('决赛场景1', '决赛场景2', '许昌试飞小场景2')))
HEIGHT_PROFILES = {
    'final_fixed': dict(zip(range(1, 7), (47., 50., 53., 34., 37., 40.))),
    'final_dual': dict(zip(range(1, 7), (47., 50., 53., 47., 50., 53.))),
}
PATH = Path(__file__).with_name('final_scenes_prepared.json')


def altitude_profile(profile):
    return 'final_fixed' if profile == 'final_scene_1' else 'final_dual'


def departure_for(profile):
    return 'fixed_xuchang' if profile == 'xuchang_small_2' else 'stadium_center'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


@lru_cache(maxsize=1)
def _prepared():
    data = json.loads(PATH.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or digest(data['scenes']) != data.get('sha256'):
        raise ValueError('决赛固定规划校验失败，请更新 final_scenes_prepared.json')
    if set(data['scenes']) != set(PROFILES):
        raise ValueError('决赛固定规划必须包含三个场景')
    return data


def load_plan(profile):
    return copy.deepcopy(_prepared()['scenes'][profile]['plan'])


def transit_geometry(profile):
    return _prepared()['scenes'][profile]['transit_geometry']
