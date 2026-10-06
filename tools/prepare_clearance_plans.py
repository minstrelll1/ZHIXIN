"""赛前重建 100/200/1000 米场景、两出发点的 5 米边界间距覆盖规划。"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.clearance_plans import PROFILES, CACHE, save_prepared_clearance_plan
from competition_backend.transit_routes import scene_plan


def main():
    for profile in PROFILES:
        for departure in ("southeast", "stadium_center"):
            raw = scene_plan(profile, departure, apply_clearance=False)
            result = save_prepared_clearance_plan(raw)
            info = result["coverage"]
            print("已完成 {}/{}：{} 个航点，最小边界间距 {:.3f} 米，遗漏面积 {:.8f} 平方米".format(
                profile, departure, info["total_scan_count"], info["minimum_route_clearance_m"],
                info["uncovered_area_m2"]), flush=True)
    print("固定覆盖规划已保存：{}".format(CACHE), flush=True)


if __name__ == "__main__":
    main()
