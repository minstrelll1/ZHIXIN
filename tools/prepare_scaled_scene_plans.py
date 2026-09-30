"""赛前生成 100m/200m 六机固定方案；比赛运行时不执行此命令。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.polygon_coverage import plan_competition_coverage
from competition_backend.scaled_scene_plans import (
    prepare_scaled_scene_plan,
    save_prepared_scaled_scene,
    load_scaled_scene_plan,
)


def main():
    base = plan_competition_coverage()
    for profile in ("outdoor100", "outdoor200"):
        for departure in ("southeast", "stadium_center"):
            plan = prepare_scaled_scene_plan(base, flight_profile=profile, departure_point=departure)
            path = save_prepared_scaled_scene(
                base, plan, flight_profile=profile, departure_point=departure,
            )
            verified = load_scaled_scene_plan(
                base, flight_profile=profile, departure_point=departure,
            )
            coverage = verified["search_area"]["coverage"]
            print("{} / {}：{}；{} 个扫描点，总航程 {:.1f} 米，预计最长 {:.1f} 秒".format(
                profile, departure, path, coverage["total_scan_count"],
                coverage["total_distance_m"], coverage["maximum_completion_time_s"],
            ))


if __name__ == "__main__":
    main()
