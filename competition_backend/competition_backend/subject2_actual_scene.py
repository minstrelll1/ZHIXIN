"""科目二固定比赛区域、禁扫圆约束及14分钟内的部分覆盖方案。"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import polygon_coverage as c
from .subject1_actual_scene import RADII_BY_UAV, REFERENCE_HEIGHTS_BY_UAV, AREA_PATH, digest

PROFILE, NAME = 'subject2_actual', '科目二比赛使用场景'
SPEED_MPS, HOVER_SECONDS, TAKEOFF_SPEED_MPS = 5., 30., 6.
MAX_MISSION_SECONDS, PLANNING_RESERVE_SECONDS = 840., 30.
MAX_SCAN_POINTS = 16
PLAN_PATH = Path(__file__).with_name('subject2_actual_prepared.json')
REGION_SOURCE = Path(__file__).with_name('subject1_actual_prepared.json')
EXCLUSION_POINT_IDS = ((2, 3, 4, 5), (1, 10, 11, 25))
UAV3_FOCUS_WGS84 = (
    (33.86414304283612, 113.70596801114152),
    (33.86260327112322, 113.7059529353448),
    (33.862565715368696, 113.70721176437122),
    (33.86418685707203, 113.70731729494827),
)
UAV35_REGION_WGS84 = (
    (33.863902022336234, 113.70398649635622),
    (33.8610689661699, 113.70360025826871),
    (33.86077496439185, 113.7070549433847),
    (33.86413365170231, 113.70731243544304),
)
UAV3_OPTIONAL_WGS84 = (33.866391666522034, 113.70856684861998)
BUILDINGS = {
    'building2': dict(latitude=33.8691414, longitude=113.7087948, uav_id=1),
    'building3': dict(latitude=33.8663255, longitude=113.7106603, uav_id=2),
    'building4': dict(latitude=33.8635343, longitude=113.7118039, uav_id=4),
}


def source_digest():
    paths = [Path(__file__), AREA_PATH, REGION_SOURCE] + [Path(__file__).with_name(name) for name in
        ('subject2_budgeted_coverage.py', 'subject2_entry_routes.py', 'subject2_rebalance.py', 'landing_avoidance.py',
         'polygon_coverage.py', 'competition_landcover.json', 'subject1_actual_scene.py')]
    return hashlib.sha256(b'\n'.join(p.read_bytes().replace(b'\r\n', b'\n') for p in paths)).hexdigest()


def local_point(projection, latitude, longitude):
    return [(latitude-projection['latitude'])*projection['north_m_per_degree'],
            -(longitude-projection['longitude'])*projection['west_m_per_degree']]


def gps_point(projection, point):
    return [projection['latitude']+point[0]/projection['north_m_per_degree'],
            projection['longitude']-point[1]/projection['west_m_per_degree']]


def update_timing(task, entry, returning, altitude):
    scans = task['waypoints_m']
    flight = [*entry, *scans[1:], *scans[-2::-1], *returning[1:]]
    distance = sum(math.dist(a, b) for a, b in zip(flight, flight[1:]))
    climb = float(altitude)/TAKEOFF_SPEED_MPS
    mission = distance/SPEED_MPS + 2*len(task['waypoints_m'])*HOVER_SECONDS + climb
    if not math.isfinite(mission) or mission > MAX_MISSION_SECONDS + 1e-6:
        raise ValueError('科目二预计起飞至返场用时 %.1f 秒，超过14分钟，不能分派' % mission)
    task.update(scan_passes=2, scan_visit_count=2*len(scans),
                scan_execution_order=list(range(len(scans)))+list(reversed(range(len(scans)))),
                scan_pass_order='forward_then_reverse', flight_path_m=flight, entry_path_m=entry, final_return_path_m=returning,
                route_primitives=[dict(kind='line', start=a, end=b, length_m=math.dist(a,b))
                                  for a,b in zip(flight,flight[1:])],
                route_distance_m=distance, travel_time_s=distance/SPEED_MPS,
                hover_scan_time_s=2*len(task['waypoints_m'])*HOVER_SECONDS,
                takeoff_time_s=climb, takeoff_speed_mps=TAKEOFF_SPEED_MPS,
                timing_altitude_m=float(altitude), mission_time_s=mission,
                mission_time_limit_s=MAX_MISSION_SECONDS,
                remaining_time_s=MAX_MISSION_SECONDS-mission)


def update_summary(area, tasks):
    values = list(tasks.values())
    area['coverage'].update(maximum_completion_time_s=max(t['mission_time_s'] for t in values),
        minimum_completion_time_s=min(t['mission_time_s'] for t in values),
        total_mission_time_s=sum(t['mission_time_s'] for t in values),
        total_distance_m=sum(t['route_distance_m'] for t in values),
        total_scan_count=sum(t['scan_count'] for t in values),
        total_scan_visit_count=sum(t['scan_visit_count'] for t in values))


def prepare_plan(progress=print):
    from shapely.geometry import Polygon, Point, LineString, mapping
    from shapely.ops import unary_union, nearest_points, split
    from .subject2_budgeted_coverage import plan_budgeted_routes
    from .subject2_entry_routes import separated_entries
    from .landing_avoidance import routes_from_home
    c._load_geometry()
    plan = json.loads(REGION_SOURCE.read_text(encoding='utf-8'))['plan']
    area = plan['search_area']; projection = area['coverage']['projection']
    boundary = area['points_m']; polygon = Polygon(boundary)
    safe = polygon.buffer(-5.03, join_style=2); depot = area['departure_point_m']
    exclusions = [Polygon([boundary[i-1] for i in ids]) for ids in EXCLUSION_POINT_IDS]
    forbidden = unary_union(exclusions)
    required, terrain, layers = c._terrain(polygon, projection, True, 5.)
    required = required.difference(forbidden)
    layers.update(required=mapping(required), excluded=mapping(polygon.difference(required)),
                  scan_forbidden=mapping(forbidden))
    regions = [Polygon(plan['planned_uavs'][str(uid)]['task']['polygon_m']) for uid in range(1,7)]
    old_focus = [local_point(projection, lat, lon) for lat, lon in UAV3_FOCUS_WGS84]
    shared = Polygon([local_point(projection, lat, lon) for lat, lon in UAV35_REGION_WGS84]).intersection(polygon)
    # 在新外框内沿原UAV3重点区的西边和南边分区，保留重点而不让两机子区越出外框。
    parts = list(split(shared, LineString(old_focus[:3])).geoms)
    anchor = Polygon(old_focus).representative_point()
    if len(parts) != 2 or any(part.geom_type != 'Polygon' or not part.is_valid for part in parts):
        raise ValueError('UAV3/UAV5共同区域不能划为两个有效子区')
    focus = next(part for part in parts if part.covers(anchor))
    regions[2] = focus
    regions[4] = next(part for part in parts if part is not focus)
    buildings, first_points = {}, {}
    for name, value in BUILDINGS.items():
        uid = value['uav_id']; point = local_point(projection,value['latitude'],value['longitude'])
        region = regions[uid-1]
        distance = region.boundary.distance(Point(point))
        if not region.contains(Point(point)) or distance <= 10.:
            raise ValueError(name+'未满足子区域内部10米要求')
        # 首点尽量靠近building；仍须满足地类及整个扫描圆禁扫约束。
        allowed = safe.intersection(region).intersection(required.buffer(-8.)).difference(
            forbidden.buffer(RADII_BY_UAV[uid]+.06))
        first = list(nearest_points(allowed, Point(point))[0].coords[0])
        if math.dist(first,point) > RADII_BY_UAV[uid] - 5.:
            raise ValueError(name+'附近没有能覆盖建筑的合法首点')
        first_points[uid] = first
        buildings[name] = dict(value, point_m=point, boundary_distance_m=distance,
                               first_waypoint_distance_m=math.dist(first,point))
    results = []
    for weight in (1.0, 1.7, 2.5):
        limits = {uid: MAX_MISSION_SECONDS-PLANNING_RESERVE_SECONDS-REFERENCE_HEIGHTS_BY_UAV[uid]/TAKEOFF_SPEED_MPS
                  for uid in range(1,7)}
        routes, metrics = plan_budgeted_routes(boundary, required, safe, regions, depot, RADII_BY_UAV,
            MAX_SCAN_POINTS, SPEED_MPS, HOVER_SECONDS, travel_weight=weight, forbidden=forbidden,
            first_points=first_points, time_limits=limits, priority_regions={3: focus})
        starts = {uid: route['waypoints_m'][0] for uid,route in enumerate(routes,1)}
        entries, entry_info = separated_entries(boundary, {uid:depot for uid in starts}, starts, reference_home=depot)
        for uid, route in enumerate(routes,1):
            returning = list(reversed(routes_from_home(boundary,depot,[route['waypoints_m'][0]],[])[0]))
            update_timing(route,entries[uid],returning,REFERENCE_HEIGHTS_BY_UAV[uid])
        results.append((routes,metrics,entry_info))
        progress('科目二航程权重 %s：%s 点，覆盖 %.2f%%，最长 %.1f 秒' %
                 (weight,[r['scan_count'] for r in routes],metrics['coverage_ratio']*100,
                  max(r['mission_time_s'] for r in routes)),flush=True)
    maximum = max(result[1]['coverage_ratio'] for result in results)
    eligible = [result for result in results if result[1]['coverage_ratio'] >= maximum-.01]
    routes, metrics, entry_info = min(eligible,key=lambda r:(max(t['mission_time_s'] for t in r[0]),
                                         sum(t['mission_time_s'] for t in r[0]),r[1]['repeated_scan_area_m2']))
    from .subject2_rebalance import rebalance
    routes, regions, final_metrics, entry_info, rebalance_info = rebalance(routes, regions, boundary,
        shared, required, safe, forbidden, depot, local_point(projection, *UAV3_OPTIONAL_WGS84),
        RADII_BY_UAV, REFERENCE_HEIGHTS_BY_UAV, update_timing)
    metrics.update(final_metrics)
    rebalance_info['requested_optional_wgs84'] = UAV3_OPTIONAL_WGS84
    rebalance_info['optional_wgs84'] = (gps_point(projection, rebalance_info['optional_point_m'])
        if rebalance_info['optional_included'] else None)
    progress('科目二最终调整：%s 点，UAV3南侧首点，北侧补扫%s，覆盖 %.2f%%，最长 %.1f 秒' %
        ([route['scan_count'] for route in routes], '保留' if rebalance_info['optional_included'] else '跳过',
         metrics['coverage_ratio']*100, max(route['mission_time_s'] for route in routes)), flush=True)
    for uid, route in enumerate(routes,1):
        task = plan['planned_uavs'][str(uid)]['task']
        region = regions[uid-1]
        minx, miny, maxx, maxy = region.bounds
        task.update(route, hover_scan_seconds=HOVER_SECONDS,
                    polygon_m=list(map(list, list(region.exterior.coords)[:-1])), area_m2=region.area,
                    bounds_m=dict(x_min=minx, x_max=maxx, y_min=miny, y_max=maxy), shape=c.shape_metrics(region),
                    waypoints_wgs84=[gps_point(projection,p) for p in route['waypoints_m']],
                    priority_buildings=[name for name,b in buildings.items() if b['uav_id']==uid])
        for point in task['waypoints_m']:
            if forbidden.distance(Point(point)) < RADII_BY_UAV[uid]:
                raise ValueError('扫描圆侵入禁扫区')
    area.update(name=NAME, flight_profile=PROFILE, terrain=terrain,terrain_layers_m=layers,
        scan_exclusion_polygons_m=[list(map(list,list(p.exterior.coords)[:-1])) for p in exclusions],
        scan_exclusion_point_ids=[list(ids) for ids in EXCLUSION_POINT_IDS], buildings=buildings,
        priority_regions=[dict(uav_id=3, name='UAV3重点区域',
            polygon_wgs84=[gps_point(projection,p) for p in list(focus.exterior.coords)[:-1]],
            polygon_m=list(map(list, list(focus.exterior.coords)[:-1])),
            coverage_ratio=unary_union([Point(p).buffer(RADII_BY_UAV[3]-.02,quad_segs=64)
                for p in routes[2]['waypoints_m']]).intersection(required.intersection(focus)).area/required.intersection(focus).area,
            scan_overlap_allowed_with_uav_ids=[5])],
        western_rebalance=rebalance_info,
        cooperative_region=dict(name='UAV3/UAV5固定范围', uav_ids=[3,5],
            requested_polygon_wgs84=UAV35_REGION_WGS84,
            polygon_m=list(map(list, list(shared.exterior.coords)[:-1])),
            polygon_wgs84=[gps_point(projection,p) for p in list(shared.exterior.coords)[:-1]],
            required_area_m2=required.intersection(shared).area,
            coverage_ratio=unary_union([Point(p).buffer(RADII_BY_UAV[uid]-.02,quad_segs=64)
                for uid in (3,5) for p in routes[uid-1]['waypoints_m']]).intersection(required.intersection(shared)).area/required.intersection(shared).area),
        entry_separation=entry_info)
    area['coverage'] = dict(metrics,projection=projection,objective='time_budgeted_marginal_coverage_with_pinned_first_scan',
        full_coverage_required=False,global_optimum_proven=False,evaluated_route_candidates=len(results),
        baseline_candidate_maximum_coverage_ratio=maximum,coverage_tradeoff_tolerance=.01,
        uav_count=6,maximum_scan_count_per_uav=MAX_SCAN_POINTS,reconnaissance_radius_m=61.,
        reconnaissance_radius_m_by_uav=RADII_BY_UAV,coverage_reference_altitude_m_by_uav=REFERENCE_HEIGHTS_BY_UAV,
        scan_passes=2,speed_mps=SPEED_MPS,hover_scan_seconds=HOVER_SECONDS,takeoff_speed_mps=TAKEOFF_SPEED_MPS,
        boundary_clearance_m=5.,area_m2=polygon.area,required_area_m2=required.area,
        excluded_area_m2=polygon.area-required.area,mission_time_limit_s=MAX_MISSION_SECONDS,
        planning_reserve_seconds=PLANNING_RESERVE_SECONDS,includes_transit=True,
        timing_note='6m/s爬升＋5m/s正逆两趟水平飞行＋每点每趟30秒，至自身降落点上方；不含下降、临时目标跟踪及飞控额外等待')
    update_summary(area,{uid:item['task'] for uid,item in plan['planned_uavs'].items()})
    stamp = source_digest()
    plan.update(subject='subject2', flight_altitude_plan='around54m',prepared_plan=dict(id=stamp[:20],
        source_digest=stamp,flight_profile=PROFILE,departure_point='stadium_center',runtime_mode='load_prepared_only',
        runtime_recalculation=False,generated_at=datetime.now(timezone.utc).isoformat(),parameters=dict(
        radius_by_uav=RADII_BY_UAV,speed=SPEED_MPS,hover=HOVER_SECONDS,takeoff_speed=TAKEOFF_SPEED_MPS,scan_passes=2,
        mission_time_limit=MAX_MISSION_SECONDS,max_scan_points=MAX_SCAN_POINTS,uav_count=6)))
    return plan


def save_plan(plan):
    payload = dict(schema_version=1,source_digest=source_digest(),plan_sha256=digest(plan),plan=plan)
    temporary = PLAN_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(PLAN_PATH)
    return PLAN_PATH


def load_plan(*, coordinate_mode='gps'):
    if coordinate_mode != 'gps':
        raise ValueError(NAME+'只能使用GPS')
    payload = json.loads(PLAN_PATH.read_text(encoding='utf-8'))
    if payload.get('schema_version') != 1 or payload.get('source_digest') != source_digest() or payload.get('plan_sha256') != digest(payload['plan']):
        raise ValueError('科目二固定规划已过期，请赛前重新生成')
    return copy.deepcopy(payload['plan'])


def finalize_dispatch(area, runtimes, actual_routes):
    """发送任何任务前联合校验实际六机进场和每架机时间预算。"""
    from .subject2_entry_routes import separated_entries
    projection = area['coverage']['projection']; homes = area.get('takeoff_gps_by_uav',{})
    active = {uid:runtime for uid,runtime in runtimes.items() if str(uid) in homes}
    if active:
        local_homes = {uid:local_point(projection,homes[str(uid)]['latitude'],homes[str(uid)]['longitude']) for uid in active}
        first = {uid:runtime.task['waypoints_m'][0] for uid,runtime in active.items()}
        limits = {}
        for uid, runtime in active.items():
            scans = runtime.task['waypoints_m']
            routes = actual_routes.get(uid)
            if routes is None:
                # 未参与分派的已连接机仍预留首航段；不用离线返程代替实际预算。
                limits[uid] = math.inf
                continue
            returning = next(r['path'] for r in routes['return_paths'] if r['source_uav_id']==uid and r['waypoint_index']==1)
            back = [local_point(projection,p[1],p[0]) for p in returning]
            back_length = sum(math.dist(a,b) for a,b in zip(back,back[1:]))
            internal = sum(math.dist(a,b) for a,b in zip(scans,scans[1:]))
            limits[uid] = (MAX_MISSION_SECONDS-2*len(scans)*HOVER_SECONDS-runtime.target_altitude_m/TAKEOFF_SPEED_MPS)*SPEED_MPS-2*internal-back_length
            if limits[uid] <= 0:
                raise ValueError('科目二爬升、两趟扫描及返程已超出14分钟，不能分派')
        entries, metadata = separated_entries(area['points_m'],local_homes,first,
            reference_home=area['departure_point_m'],maximum_lengths=limits)
        area['entry_separation'] = metadata
        for uid, runtime in active.items():
            if uid not in actual_routes:
                continue
            route = actual_routes[uid]
            route['entry_path'] = [list(reversed(gps_point(projection,p))) for p in entries[uid]]
            route['entry_path'][0] = [homes[str(uid)]['longitude'],homes[str(uid)]['latitude']]
            route['entry_path'][-1] = list(reversed(runtime.task['waypoints_wgs84'][0]))
    for uid, runtime in runtimes.items():
        task = runtime.task
        routes = actual_routes.get(uid) or task['transit_routes']
        entry = [local_point(projection,p[1],p[0]) for p in routes['entry_path']]
        returning = next(r['path'] for r in routes['return_paths']
                         if r['source_uav_id']==uid and r['waypoint_index']==1)
        returning = [local_point(projection,p[1],p[0]) for p in returning]
        update_timing(task,entry,returning,runtime.target_altitude_m)
        task['transit_routes'] = copy.deepcopy(routes)
    if runtimes:
        update_summary(area,{uid:runtime.task for uid,runtime in runtimes.items()})
