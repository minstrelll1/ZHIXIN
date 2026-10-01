"""赛前生成所有固定场景的进场及逐航点返航航线。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'competition_backend'))
from competition_backend.transit_routes import save_all_routes, CACHE

if __name__ == '__main__':
    result = save_all_routes()
    print('已保存 %d 个场景/出发点组合：%s' % (len(result['scenes']), CACHE))
