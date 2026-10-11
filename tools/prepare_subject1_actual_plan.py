"""仅赛前运行：正式比赛覆盖及操场参考进返场方案；其他场景保持原文件内容。"""
import hashlib
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))
from competition_backend.subject1_actual_scene import prepare_plan, save_plan, load_plan
from competition_backend.transit_routes import CACHE, prepare_routes

def save_transit(plan):
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    data["scenes"]["subject1_actual/stadium_center"] = prepare_routes(plan)
    data["sha256"] = hashlib.sha256(json.dumps(data["scenes"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    temp = CACHE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(CACHE)

if __name__ == "__main__":
    if "--transit-only" not in sys.argv:
        print(save_plan(prepare_plan()), flush=True)
    save_transit(load_plan())
    print("正式比赛固定航点与进返场航线保存完成", flush=True)
