from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Sequence, Tuple


Point = Tuple[float, float]


def _inclusive_values(start: float, stop: float, spacing: float) -> List[float]:
    """Return lane coordinates including both rectangle edges."""
    if spacing <= 0.0:
        raise ValueError("lane_spacing_m must be positive")
    distance = stop - start
    count = max(1, int(math.ceil(distance / spacing)))
    return [start + distance * index / count for index in range(count + 1)]


def plan_rectangular_search(
    uav_ids: Iterable[int],
    width_m: float,
    height_m: float,
    lane_spacing_m: float,
    origin_x_m: float = 0.0,
    origin_y_m: float = 0.0,
    columns: int = 3,
    rows: int = 2,
    sector_by_uav: Dict[int, int] = None,
) -> Dict[int, Dict[str, Any]]:
    """Plan a 3x2 ENU grid drawn with bottom-right origin, +X up and +Y left.

    The search rectangle therefore occupies X=[origin_x, origin_x+height] and
    Y=[origin_y, origin_y+width]. Sector numbers still run visually from the
    top-left to the bottom-right.
    """
    ids = list(uav_ids)
    if width_m <= 0.0 or height_m <= 0.0:
        raise ValueError("search area width and height must be positive")
    if columns * rows != len(ids):
        raise ValueError("search grid must contain exactly one cell per UAV")
    assignments = sector_by_uav or {
        uav_id: index + 1 for index, uav_id in enumerate(ids)
    }
    assignments = {int(key): int(value) for key, value in assignments.items()}
    if set(assignments) != set(ids) or set(assignments.values()) != set(
        range(1, len(ids) + 1)
    ):
        raise ValueError("sector_by_uav must assign every sector and UAV exactly once")

    cell_y_span = width_m / columns
    cell_x_span = height_m / rows
    tasks: Dict[int, Dict[str, Any]] = {}
    for uav_id in ids:
        sector = assignments[uav_id]
        index = sector - 1
        row_from_top = index // columns
        column = index % columns
        row_from_bottom = rows - 1 - row_from_top
        x_min = origin_x_m + row_from_bottom * cell_x_span
        x_max = x_min + cell_x_span
        y_min = origin_y_m + width_m - (column + 1) * cell_y_span
        y_max = y_min + cell_y_span

        lanes = _inclusive_values(x_min, x_max, lane_spacing_m)
        waypoints: List[List[float]] = []
        for lane_index, x_value in enumerate(lanes):
            endpoints = (y_max, y_min) if lane_index % 2 == 0 else (y_min, y_max)
            waypoints.extend([[round(x_value, 6), round(y, 6)] for y in endpoints])

        tasks[uav_id] = {
            "type": "lawnmower_search",
            "coordinate_frame": "ENU",
            "coordinate_layout": {
                "origin_corner": "bottom_right",
                "vertical_axis": "positive_x_up",
                "horizontal_axis": "positive_y_left",
            },
            "sector": sector,
            "bounds_m": {
                "x_min": round(x_min, 6),
                "x_max": round(x_max, 6),
                "y_min": round(y_min, 6),
                "y_max": round(y_max, 6),
            },
            "lane_spacing_m": lane_spacing_m,
            "waypoints_m": waypoints,
        }
    return tasks


def plan_landing_points(
    uav_ids: Iterable[int],
    landing_altitudes: Dict[int, float],
    origin_x_m: float,
    origin_y_m: float,
    width_m: float,
    height_m: float,
) -> Dict[int, Dict[str, Any]]:
    """Allocate six distinct landing points and an altitude-safe order.

    Points are laid out in a 3x2 grid.  UAVs are ordered by target altitude
    (then ID), so lower aircraft are assigned the earlier landing sequence.
    The coordinate convention is the same as the search planner: +X north/up
    and +Y west/left.
    """
    ids = list(uav_ids)
    if len(ids) != 6 or width_m <= 0.0 or height_m <= 0.0:
        raise ValueError("landing area must be positive and contain six UAVs")
    dx = height_m / 2.0
    dy = width_m / 3.0
    points = [
        [round(origin_x_m + row * dx + dx / 2.0, 6),
         round(origin_y_m + col * dy + dy / 2.0, 6)]
        for row in range(2) for col in range(3)
    ]
    ordered = sorted(ids, key=lambda uav_id: (float(landing_altitudes[uav_id]), uav_id))
    return {
        uav_id: {
            "point_m": points[index],
            "landing_sequence": index + 1,
            "landing_altitude_m": float(landing_altitudes[uav_id]),
        }
        for index, uav_id in enumerate(ordered)
    }


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _polygon_area(points: Sequence[Point]) -> float:
    return 0.5 * sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )


def _normalise_quadrilateral(points: Sequence[Sequence[float]]) -> List[Point]:
    """Validate a convex quadrilateral and return its perimeter order."""
    if len(points) != 4:
        raise ValueError("area and landing zone must each contain exactly four points")
    result = [(float(point[0]), float(point[1])) for point in points]
    if not all(math.isfinite(value) for point in result for value in point):
        raise ValueError("quadrilateral coordinates must be finite")
    if abs(_polygon_area(result)) < 1e-6:
        raise ValueError("quadrilateral is degenerate")
    signs = []
    for index in range(4):
        a, b, c = result[index], result[(index + 1) % 4], result[(index + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) < 1e-8:
            raise ValueError("quadrilateral may not contain collinear adjacent points")
        signs.append(cross > 0)
    if any(sign != signs[0] for sign in signs[1:]):
        raise ValueError("quadrilateral must be convex and points must follow its perimeter")
    return result


def _scan_line_segment(polygon: Sequence[Point], heading: float, offset: float):
    """Clip the across-track scan line to a convex polygon."""
    nx, ny = math.cos(heading + math.pi / 2.0), math.sin(heading + math.pi / 2.0)
    dx, dy = math.cos(heading), math.sin(heading)
    values = []
    for index, start in enumerate(polygon):
        end = polygon[(index + 1) % len(polygon)]
        first = start[0] * nx + start[1] * ny - offset
        second = end[0] * nx + end[1] * ny - offset
        if (first <= 1e-8 <= second) or (second <= 1e-8 <= first):
            if abs(first - second) < 1e-10:
                continue
            ratio = first / (first - second)
            point = (start[0] + ratio * (end[0] - start[0]), start[1] + ratio * (end[1] - start[1]))
            values.append(point[0] * dx + point[1] * dy)
    if len(values) < 2:
        return None
    low, high = min(values), max(values)
    if high - low < 1e-6:
        return None
    return ((dx * low + nx * offset, dy * low + ny * offset),
            (dx * high + nx * offset, dy * high + ny * offset))


def _coverage_lanes(polygon: Sequence[Point], heading: float, spacing_m: float):
    nx, ny = math.cos(heading + math.pi / 2.0), math.sin(heading + math.pi / 2.0)
    projections = [point[0] * nx + point[1] * ny for point in polygon]
    low, high = min(projections), max(projections)
    count = max(1, int(math.ceil((high - low) / spacing_m)))
    first = (low + high - (count - 1) * spacing_m) / 2.0
    lanes = [_scan_line_segment(polygon, heading, first + index * spacing_m) for index in range(count)]
    return [lane for lane in lanes if lane is not None]


def _route_for_lanes(lanes, landing_point: Point, turn_radius_m: float):
    """Choose the shorter of two alternating sweep directions.

    The score includes travel from and back to this UAV's landing point.  A
    connector is modelled as two tangents and a semicircle of the requested
    radius, rather than a zero-radius corner.
    """
    candidates = []
    for forward_first in (False, True):
        route = []
        for index, (a, b) in enumerate(lanes):
            forward = forward_first if index % 2 == 0 else not forward_first
            route.extend((a, b) if forward else (b, a))
        distance = _distance(landing_point, route[0]) + _distance(route[-1], landing_point)
        distance += sum(_distance(route[index], route[index + 1]) for index in range(0, len(route), 2))
        for index in range(1, len(route) - 1, 2):
            direct = _distance(route[index], route[index + 1])
            distance += max(0.0, direct - 2.0 * turn_radius_m) + math.pi * turn_radius_m
        candidates.append((distance, route))
    return min(candidates, key=lambda item: item[0])


def _partition_lanes(lanes, uav_ids: Sequence[int], landing_points: Dict[int, Point], turn_radius_m: float):
    """Optimally allocate contiguous coverage strips to individual aircraft.

    A state contains both the consumed lane prefix and the set of aircraft
    already assigned.  It therefore considers every UAV-to-strip matching,
    rather than assuming that UAV ID order must match the geometric strip
    order.  For a fixed scan direction this is exact dynamic programming: it
    minimises the longest individual mission first and the fleet total second.
    """
    ids = [int(value) for value in uav_ids]
    aircraft_count = min(len(ids), len(lanes))
    costs = {}
    for uav_id in ids:
        for start in range(len(lanes)):
            for end in range(start + 1, len(lanes) + 1):
                costs[uav_id, start, end] = _route_for_lanes(
                    lanes[start:end], landing_points[uav_id], turn_radius_m
                )

    # (used-UAV bit mask, consumed lane count) -> (longest, total, assignments)
    # Each assignment is (uav_id, lane_start, lane_end).
    dp = {(0, 0): (0.0, 0.0, [])}
    for group_count in range(aircraft_count):
        next_dp = {}
        remaining_groups = aircraft_count - group_count - 1
        for (mask, start), earlier in dp.items():
            for aircraft_index, uav_id in enumerate(ids):
                if mask & (1 << aircraft_index):
                    continue
                # Leave at least one lane for every remaining aircraft.
                for end in range(start + 1, len(lanes) - remaining_groups + 1):
                    distance, _ = costs[uav_id, start, end]
                    candidate = (
                        max(earlier[0], distance),
                        earlier[1] + distance,
                        earlier[2] + [(uav_id, start, end)],
                    )
                    key = (mask | (1 << aircraft_index), end)
                    saved = next_dp.get(key)
                    if saved is None or candidate[:2] < saved[:2]:
                        next_dp[key] = candidate
        dp = next_dp

    completed = [
        value for (mask, used), value in dp.items()
        if used == len(lanes) and bin(mask).count("1") == aircraft_count
    ]
    if not completed:
        raise ValueError("unable to allocate coverage lanes")
    return min(completed, key=lambda item: (item[0], item[1]))


def plan_quadrilateral_search(
    uav_ids: Iterable[int],
    area_points_m: Sequence[Sequence[float]],
    landing_points_by_uav: Dict[int, Sequence[float]],
    lane_spacing_m: float = 150.0,
    turn_radius_m: float = 5.0,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    """Plan balanced multi-UAV coverage for an arbitrary convex quadrilateral.

    Scan headings include every polygon-edge direction and a one-degree global
    search.  For each heading, contiguous strips are split with exact dynamic
    programming.  The returned route distance includes the given turning radius
    and transfer to each UAV's assigned landing point.
    """
    ids = [int(value) for value in uav_ids]
    if lane_spacing_m <= 0.0 or turn_radius_m < 0.0:
        raise ValueError("lane_spacing_m must be positive and turn_radius_m must be non-negative")
    polygon = _normalise_quadrilateral(area_points_m)
    landing = {int(key): (float(value[0]), float(value[1])) for key, value in landing_points_by_uav.items()}
    if set(landing) != set(ids):
        raise ValueError("landing point assignment must contain every UAV")
    headings = {math.radians(degree) for degree in range(0, 180)}
    for index, point in enumerate(polygon):
        next_point = polygon[(index + 1) % 4]
        angle = math.atan2(next_point[1] - point[1], next_point[0] - point[0])
        headings.update((angle % math.pi, (angle + math.pi / 2.0) % math.pi))
    candidates = []
    for heading in sorted(headings):
        lanes = _coverage_lanes(polygon, heading, lane_spacing_m)
        if not lanes:
            continue
        longest, total, ranges = _partition_lanes(lanes, ids, landing, turn_radius_m)
        candidates.append((longest, total, heading, lanes, ranges))
    if not candidates:
        raise ValueError("no coverage lanes fit inside the quadrilateral")
    longest, total, heading, lanes, ranges = min(candidates, key=lambda item: (item[0], item[1]))
    low_x, high_x = min(point[0] for point in polygon), max(point[0] for point in polygon)
    low_y, high_y = min(point[1] for point in polygon), max(point[1] for point in polygon)
    tasks: Dict[int, Dict[str, Any]] = {}
    ranges_by_uav = {uav_id: (start, end) for uav_id, start, end in ranges}
    for index, uav_id in enumerate(ids):
        if uav_id in ranges_by_uav:
            start, end = ranges_by_uav[uav_id]
            distance, route = _route_for_lanes(lanes[start:end], landing[uav_id], turn_radius_m)
            lane_count = end - start
        else:
            route, distance, lane_count = [], 0.0, 0
        tasks[uav_id] = {
            "type": "lawnmower_search",
            "coordinate_frame": "ENU",
            "sector": index + 1,
            "polygon_m": [[round(x, 6), round(y, 6)] for x, y in polygon],
            "bounds_m": {"x_min": round(low_x, 6), "x_max": round(high_x, 6),
                         "y_min": round(low_y, 6), "y_max": round(high_y, 6)},
            "lane_spacing_m": float(lane_spacing_m),
            "turn_radius_m": float(turn_radius_m),
            "lane_count": lane_count,
            "route_distance_m": round(distance, 6),
            "waypoints_m": [[round(x, 6), round(y, 6)] for x, y in route],
        }
    return tasks, {
        "polygon_m": [[round(x, 6), round(y, 6)] for x, y in polygon],
        "orientation_rad": round(heading, 8),
        "lane_count": len(lanes),
        "maximum_aircraft_distance_m": round(longest, 6),
        "total_distance_m": round(total, 6),
    }


def plan_quadrilateral_landing_points(
    uav_ids: Iterable[int], landing_altitudes: Dict[int, float], landing_points_m: Sequence[Sequence[float]]
) -> Dict[int, Dict[str, Any]]:
    """Place six landing points inside an arbitrary convex quadrilateral."""
    ids = [int(value) for value in uav_ids]
    polygon = _normalise_quadrilateral(landing_points_m)
    if len(ids) != 6:
        raise ValueError("landing plan requires six UAVs")
    # Bilinear interpolation gives a stable two-by-three layout for any convex
    # four-corner landing zone.  Input point order is P1→P2→P3→P4 around edge.
    p00, p10, p11, p01 = polygon
    def interpolate(row: int, column: int) -> Point:
        u, v = (column + 0.5) / 3.0, (row + 0.5) / 2.0
        return (
            (1-u)*(1-v)*p00[0] + u*(1-v)*p10[0] + u*v*p11[0] + (1-u)*v*p01[0],
            (1-u)*(1-v)*p00[1] + u*(1-v)*p10[1] + u*v*p11[1] + (1-u)*v*p01[1],
        )
    candidates = [interpolate(row, column) for row in range(2) for column in range(3)]
    ordered = sorted(ids, key=lambda value: (float(landing_altitudes[value]), value))
    return {
        uav_id: {"point_m": [round(candidates[index][0], 6), round(candidates[index][1], 6)],
                 "landing_sequence": index + 1, "landing_altitude_m": float(landing_altitudes[uav_id])}
        for index, uav_id in enumerate(ordered)
    }
