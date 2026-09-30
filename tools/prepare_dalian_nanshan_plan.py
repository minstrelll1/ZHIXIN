"""赛前离线生成并校验大连南山坡六机固定方案。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.fixed_gps_scene import (
    load_dalian_nanshan_plan,
    prepare_dalian_nanshan_plan,
    save_dalian_nanshan_plan,
)


def main():
    path = save_dalian_nanshan_plan(prepare_dalian_nanshan_plan())
    plan = load_dalian_nanshan_plan()
    summary = plan["search_area"]["coverage"]
    print("大连南山坡固定六机方案：{}".format(path))
    print("扫描点 {} 个，总航程 {:.1f} 米，六机总机时 {:.1f} 秒，最长单机 {:.1f} 秒".format(
        summary["total_scan_count"], summary["total_distance_m"],
        summary["total_mission_time_s"], summary["maximum_completion_time_s"],
    ))


if __name__ == "__main__":
    main()
