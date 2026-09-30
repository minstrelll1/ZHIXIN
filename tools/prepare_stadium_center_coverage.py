"""赛前生成操场中央起降参考的固定规划；比赛运行时只读取结果。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.polygon_coverage import plan_competition_coverage, save_prepared_plan
from competition_backend.stadium_departure import (
    prepare_stadium_center_plan,
    save_prepared_stadium_plan,
)


def main():
    parser = argparse.ArgumentParser(description="赛前生成操场中央固定六机方案")
    parser.add_argument("--profile", choices=("all", "competition", "lab", "outdoor5", "lab10"), default="all")
    parser.add_argument("--radius", type=float, help="所选场景侦察半径；比赛默认 75 米，缩小场景默认 1 米")
    parser.add_argument("--speed", type=float, help="所选场景飞行速度；比赛默认 5 米/秒，缩小场景按配置")
    parser.add_argument("--hover", type=float, default=10.0, help="每点悬停扫描秒数，默认 10 秒")
    parser.add_argument("--aspect", type=float, default=2.0, help="子区长宽比限制，默认 2")
    parser.add_argument("--forest-edge", type=float, default=5.0, help="林缘保留米数，默认 5 米")
    parser.add_argument("--no-terrain", action="store_true", help="关闭地类免侦察")
    parser.add_argument("--uav-count", type=int, default=6)
    args = parser.parse_args()
    if args.profile == "all" and (args.radius is not None or args.speed is not None):
        parser.error("自定义侦察半径或速度时须指定一个 --profile")
    # --radius/--speed 对正式比赛是比赛尺度参数；对缩小场景是缩放后的参数。
    # 若改变基础参数，先离线生成基础快照，再生成中央快照；网页运行时只读取。
    base_radius = args.radius if args.profile == "competition" and args.radius is not None else 75.0
    base_speed = args.speed if args.profile == "competition" and args.speed is not None else 5.0
    custom_base = (base_radius != 75.0 or base_speed != 5.0 or args.hover != 10.0
                   or args.aspect != 2.0 or args.forest_edge != 5.0
                   or args.no_terrain or args.uav_count != 6)
    base = plan_competition_coverage(
        radius=base_radius, speed_mps=base_speed, hover_seconds=args.hover,
        max_region_aspect_ratio=args.aspect, forest_edge_m=args.forest_edge,
        terrain_exclusions_enabled=not args.no_terrain, uav_count=args.uav_count,
        rebuild=custom_base,
    )
    if custom_base:
        print("基础固定方案已保存：{}".format(save_prepared_plan(base)))
    profiles = ("competition", "lab", "outdoor5", "lab10") if args.profile == "all" else (args.profile,)
    for profile in profiles:
        plan = prepare_stadium_center_plan(base, profile, args.radius, args.speed)
        path = save_prepared_stadium_plan(base, plan, profile, args.radius, args.speed)
        coverage = plan["search_area"]["coverage"]
        scale = plan["prepared_plan"]["optimized_scenario"]["scale"]
        print(
            "{} 固定方案已保存：{}；六机预计用时差 {:.1f} 秒；总航程 {:.1f} 米".format(
                profile, path, coverage["completion_time_spread_s"],
                coverage["total_distance_m"] * scale,
            )
        )


if __name__ == "__main__":
    main()
