"""把四份已校验的操场中央固定方案导出为可读的航点报告。"""
import itertools
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))

from competition_backend.polygon_coverage import adapt_competition_plan, plan_competition_coverage
from competition_backend import polygon_coverage as coverage_module
from competition_backend.stadium_departure import (
    STADIUM_CENTER_WGS84, _stadium_local, anchor_stadium_plan, load_prepared_stadium_plan,
)


def main():
    base = plan_competition_coverage()
    coverage_module._load_geometry()
    old_polygon = coverage_module.Polygon(base["search_area"]["points_m"])
    old_depot = _stadium_local(base)
    old_closed_distance = 0.0
    old_closed_times = []
    for wrapper in base["planned_uavs"].values():
        task = wrapper["task"]
        flight_path = task["flight_path_m"]
        def link_length(a, b):
            return sum(math.dist(start, end) for start, end in itertools.pairwise(
                coverage_module._shortest_link(a, b, old_polygon)
            ))
        distance = (float(task["route_distance_m"])
                    + link_length(old_depot, flight_path[0])
                    + link_length(flight_path[-1], old_depot))
        old_closed_distance += distance
        old_closed_times.append(distance / 5.0 + int(task["scan_count"]) * 10.0)
    scenes = [
        ("competition", "正式比赛区域", None, None, "gps", 3.0),
        ("lab", "3 米实验室", 1.0, 0.2, "xyz", 3.0),
        ("outdoor5", "5 米小场景", 1.0, 0.2, "xyz", 5.0),
        ("lab10", "10 米小场景", 1.0, 0.5, "xyz", 10.0),
    ]
    rows = [
        "# 操场中央出发 · 科目一、二六机固定规划",
        "",
        "操场中央参考点约为 **WGS84 北纬 {:.5f}°、东经 {:.5f}°**。此位置由 2017 年 Esri 历史卫星影像估算，非 GNSS 实测起降点。任务执行时六架无人机各自记录真实起飞位置并返回各自的 home；本方案不会向六机下发同一个静态降落坐标。请在实飞前核对起降坪、间隔与航线。".format(
            STADIUM_CENTER_WGS84["latitude"], STADIUM_CENTER_WGS84["longitude"]
        ),
        "",
        "该方案在赛前预计算并存为本地 JSON；比赛运行时只加载和校验，不重复求解。使用紧凑分区候选、带权分区调整、离散圆覆盖和闭合航线 2-opt 启发式，**未证明全局最优或六机用时严格相等**。预计时间 = 从中心参考点往返的水平航程 ÷ 速度 + 悬停扫描次数 × 单次扫描时间；不含起飞、爬升、加减速、避障和实际起降偏差。",
        "",
        "| 场景 | 速度 | 侦察半径 | 扫描总次数 | 总航程（含估计往返） | 六机预计用时范围 | 时间极差 | 未覆盖面积 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    details = []
    competition_center_distance = None
    for profile, name, radius, speed, mode, extent in scenes:
        source = load_prepared_stadium_plan(base, profile, radius, speed)
        adapted = adapt_competition_plan(
            source, coordinate_mode=mode, flight_profile=profile,
            max_extent_m=extent,
            lab_radius_m=radius if radius is not None else 1.0,
            lab_speed_mps=speed if speed is not None else 0.2,
            lab_hover_seconds=10.0,
        )
        plan = anchor_stadium_plan(adapted, source)
        area = plan["search_area"]
        c = area["coverage"]
        if profile == "competition":
            competition_center_distance = c["total_distance_m"]
        details.extend([
            "",
            "## {}".format(name),
            "",
            "方案文件：`competition_backend/competition_backend/competition_coverage_stadium_center{}.json`。坐标约定：X 向北、Y 向西；正式比赛航点另列 WGS84 经度/纬度，飞行高度由页面所选高度方案填入。".format("" if profile == "competition" else "_" + profile),
            "",
            "| 无人机 | 子区面积（平方米） | 扫描点 | 含估计往返航程（米） | 预计执行时间（秒） |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        times = []
        for uav_id in range(1, 7):
            task = plan["planned_uavs"][str(uav_id)]["task"]
            times.append(task["mission_time_s"])
            details.append("| UAV{} | {:.2f} | {} | {:.2f} | {:.2f} |".format(
                uav_id, task["area_m2"] * (extent / max(float(source["search_area"]["width_m"]), float(source["search_area"]["height_m"]))) ** 2 if profile != "competition" else task["area_m2"],
                task["scan_count"], task["route_distance_m"], task["mission_time_s"],
            ))
        for uav_id in range(1, 7):
            task = plan["planned_uavs"][str(uav_id)]["task"]
            details.extend(["", "<details><summary>UAV{} 子区边界与 {} 个扫描航点</summary>".format(uav_id, task["scan_count"]), ""])
            details.append("子区边界（X 北/Y 西，米）：" + "；".join("({:.3f}, {:.3f})".format(*point) for point in task["polygon_m"]))
            details.extend([
                "",
                "| 序号 | X 北（米） | Y 西（米） | 经度（度） | 纬度（度） |" if mode == "gps"
                else "| 序号 | X 北（米） | Y 西（米） |",
                "| ---: | ---: | ---: | ---: | ---: |" if mode == "gps"
                else "| ---: | ---: | ---: |",
            ])
            for index, point in enumerate(task["waypoints_m"], 1):
                if mode == "gps":
                    lat, lon = task["waypoints_wgs84"][index - 1][:2]
                    details.append("| {} | {:.3f} | {:.3f} | {:.9f} | {:.9f} |".format(
                        index, point[0], point[1], lon, lat,
                    ))
                else:
                    details.append("| {} | {:.3f} | {:.3f} |".format(index, point[0], point[1]))
            details.extend(["", "</details>"])
        rows.append("| {} | {:.2f} m/s | {:.2f} m | {} | {:.2f} m | {:.2f}–{:.2f} s | {:.2f} s | {:.6f} m² |".format(
            name, c["speed_mps"], c["reconnaissance_radius_m"], c["total_scan_count"],
            c["total_distance_m"], min(times), max(times), max(times) - min(times), c["uncovered_area_m2"],
        ))
    rows.extend([
        "",
        "旧版区域右下角固定方案保持不变。其 `route_distance_m` 只计子区内扫描航程，**不含起降往返**；上表中央方案计入从估算中心的往返，因此两种数字不能直接当作同口径的优劣比较。实际起降点并未由历史影像自动确定。",
        "",
        "若只把旧版六条固定扫描路线的两端接回**同一个估算中心**、不重新分区或调序，参考总航程约 {:.2f} 米、六机预计用时极差约 {:.2f} 秒；本次中央启发式方案约 {:.2f} 米。这个同口径参考比较说明当前方案比原路线简单接回中心短约 {:.2f} 米，但不能证明全局最短或实飞时间相同。".format(
            old_closed_distance, max(old_closed_times) - min(old_closed_times),
            competition_center_distance, old_closed_distance - competition_center_distance,
        ),
        *details,
        "",
    ])
    output = ROOT / "docs" / "stadium_center_coverage_plan.md"
    output.write_text("\n".join(rows), encoding="utf-8")
    print("已导出：{}".format(output))


if __name__ == "__main__":
    main()
