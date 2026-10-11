"""正式科目一固定WGS84场地：每机最多16点的高效覆盖，运行时只读方案。"""
from __future__ import annotations
import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from . import polygon_coverage as c
from .stadium_departure import STADIUM_CENTER_WGS84, _power_cells
from .budgeted_coverage import plan_budgeted_routes

PROFILE = 'subject1_actual'
NAME = '科目一比赛使用场景'
RADIUS_M, SPEED_MPS, HOVER_SECONDS = 61.0, 5.0, 40.0
RADII_BY_UAV = dict(zip(range(1,7), (61.,58.,55.,52.,50.,45.)))
REFERENCE_HEIGHTS_BY_UAV = dict(zip(range(1,7), (59.,56.,53.,50.,47.,44.)))
MAX_SCAN_POINTS = 16
CLEARANCE_M, PLANNING_CLEARANCE_M = 5.0, 5.03
OTHER_HOME_CLEARANCE_M = 2.5
AREA_PATH = Path(__file__).with_name('subject1_actual_area.json')
PLAN_PATH = Path(__file__).with_name('subject1_actual_prepared.json')
PARTITION_PATH = Path(__file__).with_name('subject1_actual_partition_reference.json')


def digest(value):
    return hashlib.sha256(c._canonical(value).encode('utf-8')).hexdigest()


def source_digest():
    paths = [Path(__file__), AREA_PATH, PARTITION_PATH] + [Path(__file__).with_name(name) for name in
        ('budgeted_coverage.py','polygon_coverage.py','stadium_departure.py','landing_avoidance.py','competition_landcover.json')]
    return hashlib.sha256(b'\n'.join(p.read_bytes().replace(b'\r\n',b'\n') for p in paths)).hexdigest()


def prepare_plan(progress=print):
    """优先新增覆盖收益；允许留白，不为全覆盖添加林缘绕行点。"""
    from shapely.geometry import Polygon, Point, LineString
    from shapely.ops import unary_union
    c._load_geometry()
    source=json.loads(AREA_PATH.read_text(encoding='utf-8'))
    points,projection=c.local_projection(source['points']);polygon=Polygon(points)
    safe=polygon.buffer(-PLANNING_CLEARANCE_M,join_style=2)
    depot=[(STADIUM_CENTER_WGS84['latitude']-projection['latitude'])*projection['north_m_per_degree'],
           -(STADIUM_CENTER_WGS84['longitude']-projection['longitude'])*projection['west_m_per_degree']]
    if not polygon.is_valid or safe.geom_type!='Polygon' or not safe.covers(Point(depot)):
        raise ValueError('正式比赛边界、5米安全区或操场中心无效')
    required,terrain,layers=c._terrain(polygon,projection,True,5.)
    reference=json.loads(PARTITION_PATH.read_text(encoding='utf-8'))
    if reference['boundary_m'] != list(map(list,points)):
        raise ValueError('限点分区参考与固定比赛区域不匹配')
    regions=[Polygon(reference['regions_m'][str(uid)]) for uid in range(1,7)]
    candidates=[regions]
    seeds=[tuple(r.centroid.coords[0]) for r in regions];weights=[0.]*6
    # 比较按扫描圆面积分担任务的紧凑分区。固定参考避免重复生成时累积漂移。
    for _ in range(4):
        trial=_power_cells(polygon,seeds,weights)
        if trial is None or any(r.geom_type!='Polygon' or r.area<18000 for r in trial):break
        candidates.append(trial)
        work=[required.intersection(r).area/RADII_BY_UAV[uid]**2 for uid,r in enumerate(trial,1)]
        mean=sum(work)/6
        weights=[w-900*(v-mean) for w,v in zip(weights,work)]
    results=[]
    for index,regions in enumerate(candidates):
        joined=unary_union(regions)
        if polygon.symmetric_difference(joined).area>1e-5 or sum(r.area for r in regions)-joined.area>1e-5:continue
        for weight in (1.,1.7):
            routes,metrics=plan_budgeted_routes(points,required,safe,regions,depot,RADII_BY_UAV,
                                                MAX_SCAN_POINTS,SPEED_MPS,HOVER_SECONDS,travel_weight=weight)
            results.append((regions,routes,metrics))
            progress('候选 {}，航程权重 {}，点数 {}，覆盖 {:.2%}，最长任务 {:.1f}s'.format(index+1,weight,
                [r['scan_count'] for r in routes],metrics['coverage_ratio'],max(r['mission_time_s'] for r in routes)),flush=True)
    if not results:raise ValueError('限点方案生成失败')
    # 接近最大覆盖（最多相差1个百分点）时，选择最早完成和更少重复扫描的方案。
    maximum=max(result[2]['coverage_ratio'] for result in results)
    eligible=[result for result in results if result[2]['coverage_ratio']>=maximum-.01]
    regions,routes,metrics=min(eligible,key=lambda r:(max(t['mission_time_s'] for t in r[1]),
        sum(t['mission_time_s'] for t in r[1]),r[2]['repeated_scan_area_m2']))
    tasks={}
    for uid,(region,route) in enumerate(zip(regions,routes),1):
        minx,miny,maxx,maxy=region.bounds
        task=dict(copy.deepcopy(route),type='lawnmower_search',coordinate_frame='LOCAL_NORTH_WEST',sector=uid,
            polygon_m=list(map(list,list(region.exterior.coords)[:-1])),area_m2=region.area,
            bounds_m=dict(x_min=minx,x_max=maxx,y_min=miny,y_max=maxy),shape=c.shape_metrics(region),
            reconnaissance_radius_m=RADII_BY_UAV[uid],reconnaissance_mode='hover_scan',speed_mps=SPEED_MPS,
            hover_scan_seconds=HOVER_SECONDS,turn_radius_m=0.,coverage_reference_altitude_m=REFERENCE_HEIGHTS_BY_UAV[uid],
            waypoints_wgs84=[[projection['latitude']+p[0]/projection['north_m_per_degree'],
                              projection['longitude']-p[1]/projection['west_m_per_degree']] for p in route['waypoints_m']])
        tasks[str(uid)]=dict(uav_id=uid,task=task)
    times=[r['mission_time_s'] for r in routes]
    minimum=min(LineString(r['flight_path_m']).distance(polygon.boundary) for r in routes)
    if minimum<CLEARANCE_M:raise ValueError('完整任务航线距离边界不足5米')
    coverage=dict(metrics,projection=projection,objective='maximize_useful_coverage_per_time_with_16_waypoint_limit',
        objective_scope='six_aircraft_unique_scanned_area_including_stadium_entry_and_return',
        algorithm='joint_marginal_disk_gain_safe_insertion_2opt_compact_partition_candidates',
        full_coverage_required=False,global_optimum_proven=False,evaluated_partitions=len(candidates),
        evaluated_route_candidates=len(results),candidate_maximum_coverage_ratio=maximum,coverage_tradeoff_tolerance=.01,
        uav_count=6,maximum_scan_count_per_uav=MAX_SCAN_POINTS,reconnaissance_radius_m=RADIUS_M,
        reconnaissance_radius_m_by_uav=RADII_BY_UAV,coverage_reference_altitude_m_by_uav=REFERENCE_HEIGHTS_BY_UAV,
        speed_mps=SPEED_MPS,hover_scan_seconds=HOVER_SECONDS,boundary_clearance_m=CLEARANCE_M,
        minimum_route_clearance_m=minimum,area_m2=polygon.area,required_area_m2=required.area,
        excluded_area_m2=polygon.area-required.area,maximum_completion_time_s=max(times),minimum_completion_time_s=min(times),
        completion_time_spread_s=max(times)-min(times),total_mission_time_s=sum(times),
        total_distance_m=sum(r['route_distance_m'] for r in routes),total_scan_count=sum(r['scan_count'] for r in routes),
        includes_transit=True,timing_note='按操场中央、5m/s及40秒/点估算；不含爬升、下降、目标跟踪；其他高度沿用航点，覆盖率按59/56/53/50/47/44米对应半径计算')
    minx,miny,maxx,maxy=polygon.bounds
    area=dict(name=NAME,coordinate_mode='gps',coordinate_frame='WGS84',flight_profile=PROFILE,
        departure_point='stadium_center',departure_point_m=depot,departure_point_wgs84=dict(STADIUM_CENTER_WGS84),
        departure_source='用户指定操场中心；沿用2017 Esri影像中心坐标',planned_departure_reference_only=True,
        landing_mode='onboard_home',home_reference_policy='first_valid_unarmed_gps_per_boot',
        other_home_clearance_m=OTHER_HOME_CLEARANCE_M,boundary_clearance_m=CLEARANCE_M,
        points=source['points'],points_m=list(map(list,points)),origin_x_m=minx,origin_y_m=miny,
        width_m=maxy-miny,height_m=maxx-minx,polygon_width_m=maxy-miny,polygon_height_m=maxx-minx,
        gps_origin=dict(latitude=projection['latitude'],longitude=projection['longitude'],source='fixed_competition_boundary'),
        terrain=terrain,terrain_layers_m=layers,coverage=coverage,turn_radius_m=0.)
    return dict(preview_only=True,subject='subject1',mission_id='coverage-preview',controller_mode='preview',
        planned_uavs=tasks,uavs={},search_area=area,prepared_plan=dict(id=source_digest()[:20],source_digest=source_digest(),
        flight_profile=PROFILE,departure_point='stadium_center',runtime_mode='load_prepared_only',runtime_recalculation=False,
        generated_at=datetime.now(timezone.utc).isoformat(),parameters=dict(radius=RADIUS_M,radius_by_uav=RADII_BY_UAV,
        speed=SPEED_MPS,hover=HOVER_SECONDS,terrain=True,uav_count=6,max_scan_points=MAX_SCAN_POINTS)))


def save_plan(plan):
    payload=dict(schema_version=1,source_digest=source_digest(),plan_sha256=digest(plan),plan=plan)
    temporary=PLAN_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(PLAN_PATH)
    return PLAN_PATH


def load_plan(*, coordinate_mode='gps'):
    if coordinate_mode!='gps':raise ValueError(NAME+'只能使用GPS')
    payload=json.loads(PLAN_PATH.read_text(encoding='utf-8'))
    if (payload.get('schema_version')!=1 or payload.get('source_digest')!=source_digest()
            or payload.get('plan_sha256')!=digest(payload['plan'])):
        raise ValueError('科目一正式比赛固定规划已过期，请赛前重新生成')
    return copy.deepcopy(payload['plan'])
