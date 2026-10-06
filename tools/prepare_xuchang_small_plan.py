"""赛前离线生成、校验许昌扣除区的固定六机方案。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.xuchang_small_scene import (
    load_xuchang_small_plan, prepare_xuchang_small_plan, save_xuchang_small_plan,
)


def main():
    path = save_xuchang_small_plan(prepare_xuchang_small_plan())
    plan = load_xuchang_small_plan()
    summary = plan["search_area"]["coverage"]
    print("许昌固定六机方案：{}".format(path))
    print("扫描点 {} 个，扣除区 {:.1f} 平方米，需侦察区 {:.1f} 平方米，总航程 {:.1f} 米".format(
        summary["total_scan_count"], summary["excluded_area_m2"],
        summary["required_area_m2"], summary["total_distance_m"]))


if __name__ == "__main__":
    main()
