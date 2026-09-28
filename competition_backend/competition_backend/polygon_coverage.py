"""六机紧凑分区、离散悬停覆盖和总机时优化；只生成预览。"""
from __future__ import annotations
import copy
import hashlib
import heapq
import json
import math
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


def _load_geometry():
    global rotate, LineString, Point, Polygon, box, unary_union, shape, mapping, transform
    from shapely.affinity import rotate
    from shapely.geometry import LineString, Point, Polygon, box, shape, mapping
    from shapely.ops import unary_union, transform


def area_preset():
    return json.loads(Path(__file__).with_name('competition_area.json').read_text(encoding='utf-8'))


def _meters_per_degree(latitude):
    """Return WGS84 north/west metres per degree at the actual GPS latitude."""
    phi = math.radians(latitude)
    e2, a = 6.6943799901413165e-3, 6378137.0
    w = math.sqrt(1 - e2 * math.sin(phi) ** 2)
    north = math.pi / 180 * a * (1 - e2) / w ** 3
    west = math.pi / 180 * a / w * math.cos(phi)
    return north, west


def local_projection(points):
    """WGS84 中纬度局部投影，X 向北、Y 向西。"""
    lat0 = sum(p[0] for p in points) / len(points)
    lon0 = sum(p[1] for p in points) / len(points)
    north, west = _meters_per_degree(lat0)
    return ([(north * (lat-lat0), -west * (lon-lon0)) for lat, lon in points],
            {'latitude':lat0, 'longitude':lon0, 'north_m_per_degree':north,
             'west_m_per_degree':west, 'method':'WGS84_local_midlatitude'})


def _parts(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [p for g in getattr(geometry, 'geoms', []) for p in _parts(g)]


def shape_metrics(region):
    rect = region.minimum_rotated_rectangle
    coords = list(rect.exterior.coords)
    lengths = sorted(math.dist(a,b) for a,b in zip(coords,coords[1:]))
    short, long = lengths[0], lengths[-1]
    return {'long_side_m':long, 'short_side_m':short, 'aspect_ratio':long/short,
            'rectangle_fill_ratio':region.area/rect.area,
            'compactness':4*math.pi*region.area/region.length**2}


def required_area(polygon, forest, water, forest_edge_m=5.0, water_margin_m=10.0):
    """只排除内部。林缘宽度在真实林地边界上计算，再裁切比赛范围。"""
    forest_inner = forest.buffer(-forest_edge_m).intersection(polygon)
    water_inner = water.buffer(-water_margin_m).intersection(polygon)
    # 林缘有目标的可能，优先级高于水体分类，即使两种来源存在交叠。
    forest_edge = forest.difference(forest.buffer(-forest_edge_m)).intersection(polygon)
    excluded = unary_union([forest_inner, water_inner]).difference(forest_edge)
    return polygon.difference(excluded), excluded, forest_edge


def _terrain(polygon, projection, enabled, forest_edge_m):
    data = json.loads(Path(__file__).with_name('competition_landcover.json').read_text(encoding='utf-8'))
    def project(lon,lat,z=None):
        return ((lat-projection['latitude'])*projection['north_m_per_degree'],
                -(lon-projection['longitude'])*projection['west_m_per_degree'])
    groups = {'forest':[], 'water':[]}
    for item in data['features']:
        kind = item['properties']['kind']
        g = transform(project, shape(item['geometry']))
        if not g.is_valid:
            raise ValueError('地类边界无效，请重新导入地图数据')
        if kind in groups:
            groups[kind].append(g)
    forest,water = (unary_union(groups[k]) for k in ('forest','water'))
    target,excluded,edge = required_area(polygon,forest,water,forest_edge_m)
    if not enabled:
        target,excluded=polygon,Polygon()
    info = dict(data['metadata'], enabled=enabled, forest_edge_m=forest_edge_m,
                water_margin_m=10.0, forest_area_m2=forest.intersection(polygon).area,
                water_area_m2=water.intersection(polygon).area,
                forest_edge_area_m2=edge.area, excluded_area_m2=excluded.area,
                required_area_m2=target.area, interpretation='地图地类推定，非现状测量')
    layers={'forest':forest.intersection(polygon), 'water':water.intersection(polygon),
            'forest_edge':edge, 'excluded':excluded, 'required':target}
    return target, info, {name:mapping(g) for name,g in layers.items()}


def _partition_candidates(polygon, max_aspect, beam=8, uav_count=6):
    """按形状筛选有限候选；面积范围避免总机时目标产生极小任务区。"""
    target_area=polygon.area/uav_count
    cache={}
    def solve(part,count):
        key=(part.wkb,count)
        if key in cache:
            return cache[key]
        if part.geom_type!='Polygon' or not part.is_valid or part.interiors:
            return []
        if not target_area*.6*count <= part.area <= target_area*1.4*count:
            return []
        if count==1:
            m=shape_metrics(part)
            if m['aspect_ratio']>max_aspect+1e-8 or m['rectangle_fill_ratio']<.45:
                return []
            # 仅用于候选裁剪；最终所有保留候选按真实飞行+扫描总时长比较。
            score=part.length+250*(m['aspect_ratio']-1)**2
            return [(score,[part])]
        choices=[]
        minx,miny,maxx,maxy=part.bounds
        for axis in (0,1):
            for allocated in sorted({count//2,(count+1)//2}):
                for factor in (.9,1.0,1.1):
                    fraction=allocated/count*factor
                    lo,hi=(minx,maxx) if axis==0 else (miny,maxy)
                    for _ in range(20):
                        cut=(lo+hi)/2
                        mask=box(minx-1,miny-1,cut,maxy+1) if axis==0 else box(minx-1,miny-1,maxx+1,cut)
                        if part.intersection(mask).area < part.area*fraction:
                            lo=cut
                        else:
                            hi=cut
                    left,right=part.intersection(mask),part.difference(mask)
                    a,b=solve(left,allocated),solve(right,count-allocated)
                    for ac,ap in a:
                        for bc,bp in b:
                            choices.append((ac+bc,ap+bp))
        choices.sort(key=lambda item:item[0])
        cache[key]=choices[:beam]
        return cache[key]
    return solve(polygon,uav_count)


def _shortest_link(start,end,region):
    allowed=region.buffer(1e-7)
    if allowed.covers(LineString([start,end])):
        return [start,end]
    nodes=[start,end]+list(region.exterior.coords)[:-1]
    distances,previous,queue={0:0.0},{},[(0.0,0)]
    while queue:
        distance,i=heapq.heappop(queue)
        if distance!=distances.get(i):
            continue
        if i==1:
            path=[end]
            while i:
                i=previous[i];path.append(nodes[i])
            return path[::-1]
        for j,point in enumerate(nodes):
            candidate=distance+math.dist(nodes[i],point)
            if i==j or candidate>=distances.get(j,math.inf):
                continue
            if allowed.covers(LineString([nodes[i],point])):
                distances[j],previous[j]=candidate,i
                heapq.heappush(queue,(candidate,j))
    raise ValueError('子区域内无法连通悬停航点')


def _scan_points(region,target,radius):
    """面积增益贪心圆覆盖，精确补齐余洞，然后剔除冗余扫描点。"""
    if target.is_empty or target.area<=1e-5:
        return []
    step=radius*.65
    minx,miny,maxx,maxy=target.bounds
    candidates=[]
    for i in range(math.ceil((maxx-minx)/step)+1):
        for j in range(math.ceil((maxy-miny)/step)+1):
            point=Point(minx+i*step,miny+j*step)
            if target.covers(point):
                candidates.append((point.x,point.y))
    for part in _parts(target):
        for ring in [part.exterior,*part.interiors]:
            count=max(1,math.ceil(ring.length/step))
            candidates.extend(tuple(ring.interpolate(ring.length*i/count).coords[0]) for i in range(count))
        candidates.append(tuple(part.representative_point().coords[0]))
    candidates=list(dict.fromkeys(candidates))
    disks=[Point(p).buffer(radius-.02,quad_segs=32) for p in candidates]
    selected=[]
    missing=target
    for _ in range(500):
        if missing.area<=1e-5:
            break
        gains=[g.intersection(missing).area for g in disks]
        best=max(range(len(gains)),key=gains.__getitem__) if gains else None
        if best is None or gains[best]<min(1e-4,missing.area*.1):
            p=tuple(max(_parts(missing),key=lambda x:x.area).representative_point().coords[0])
            disk=Point(p).buffer(radius-.02,quad_segs=32)
        else:
            p=candidates.pop(best);disk=disks.pop(best)
        selected.append(p);missing=missing.difference(disk)
    else:
        raise ValueError('悬停覆盖点过多，无法完成规划')
    # 删除不会破坏全覆盖的冗余点；扫描时间为正，删点一定不会增加最优路程。
    for i in range(len(selected)-1,-1,-1):
        remaining=selected[:i]+selected[i+1:]
        if remaining and target.difference(unary_union([Point(p).buffer(radius-.02,quad_segs=32) for p in remaining])).area<=1e-5:
            selected=remaining
    return selected


def _hover_route(region,target,radius,speed,hover):
    points=_scan_points(region,target,radius)
    if not points:
        return {'waypoints_m':[], 'scan_waypoints_m':[], 'flight_path_m':[],
                'route_primitives':[], 'route_distance_m':0., 'scan_count':0,
                'travel_time_s':0., 'hover_scan_time_s':0., 'mission_time_s':0.,
                'coverage_ratio':1., 'uncovered_area_m2':0.}
    links={}
    def link(i,j):
        if (i,j) not in links:
            path=_shortest_link(points[i],points[j],region)
            length=sum(math.dist(a,b) for a,b in zip(path,path[1:]))
            links[i,j]=(length,path);links[j,i]=(length,path[::-1])
        return links[i,j]
    def distance(order):
        return sum(link(i,j)[0] for i,j in zip(order,order[1:]))
    best=None
    for start in range(len(points)):
        order=[start];remaining=set(range(len(points)))-{start}
        while remaining:
            next_id=min(remaining,key=lambda j:(link(order[-1],j)[0],j))
            order.append(next_id);remaining.remove(next_id)
        length=distance(order)
        for _ in range(4):
            changed=False
            for a in range(len(order)-1):
                for b in range(a+1,len(order)):
                    candidate=order[:a]+order[a:b+1][::-1]+order[b+1:]
                    value=distance(candidate)
                    if value<length-1e-6:
                        order,length=candidate,value;changed=True
            if not changed:
                break
        if best is None or length<best[0]:
            best=(length,order)
    length,order=best
    scans=[list(points[i]) for i in order]
    flight=[scans[0]]
    for i,j in zip(order,order[1:]):
        flight.extend([list(p) for p in link(i,j)[1][1:]])
    covered=unary_union([Point(p).buffer(radius-.02,quad_segs=32) for p in scans])
    missed=target.difference(covered).area
    if missed>1e-5 or not all(region.buffer(1e-6).covers(Point(p)) for p in scans):
        raise ValueError('悬停覆盖或航点边界校验失败')
    return {'waypoints_m':scans,'scan_waypoints_m':copy.deepcopy(scans),'flight_path_m':flight,
            'route_primitives':[{'kind':'line','start':a,'end':b,'length_m':math.dist(a,b)} for a,b in zip(flight,flight[1:])],
            'route_distance_m':length,'scan_count':len(scans),'travel_time_s':length/speed,
            'hover_scan_time_s':len(scans)*hover,'mission_time_s':length/speed+len(scans)*hover,
            'coverage_ratio':1.,'uncovered_area_m2':missed}


@lru_cache(maxsize=4)
def _compute(radius,speed,hover,max_aspect,forest_edge,terrain_enabled,digest,uav_count=6):
    _load_geometry()
    preset=area_preset();points,projection=local_projection(preset['points']);polygon=Polygon(points)
    required,terrain,layers=_terrain(polygon,projection,terrain_enabled,forest_edge)
    candidates=[]
    for heading in (0,-6,6,30,60):
        rotated=rotate(polygon,-heading,origin=(0,0))
        for score,parts in _partition_candidates(rotated,max_aspect,uav_count=uav_count):
            candidates.append((score,heading,[rotate(p,heading,origin=(0,0)) for p in parts]))
    if not candidates:
        raise ValueError(f'当前长宽比限制下未找到 {uav_count} 个连通分区，请在赛前调整参数')
    # 每个方向保留八组，均计算真实覆盖路径和扫描时间。
    best=None;route_cache={}
    for _,heading,parts in candidates:
        routes=[]
        for part in parts:
            key=part.wkb
            if key not in route_cache:
                route_cache[key]=_hover_route(part,required.intersection(part),radius,speed,hover)
            routes.append(route_cache[key])
        cost=(sum(r['mission_time_s'] for r in routes),max(r['mission_time_s'] for r in routes),sum(p.length for p in parts))
        if best is None or cost<best[0]:
            best=(cost,heading,parts,routes)
    cost,heading,parts,routes=best
    joined=unary_union(parts)
    if polygon.symmetric_difference(joined).area>1e-5 or sum(p.area for p in parts)-joined.area>1e-5:
        raise ValueError('六区完整性校验失败')
    tasks={}
    for i,(part,route) in enumerate(sorted(zip(parts,routes),key=lambda item:(-item[0].centroid.x,item[0].centroid.y)),1):
        minx,miny,maxx,maxy=part.bounds
        task=dict(route,type='lawnmower_search',coordinate_frame='LOCAL_NORTH_WEST',sector=i,
                  polygon_m=list(part.exterior.coords)[:-1],area_m2=part.area,
                  required_area_m2=part.intersection(required).area,
                  bounds_m={'x_min':minx,'x_max':maxx,'y_min':miny,'y_max':maxy},
                  shape=shape_metrics(part),reconnaissance_radius_m=radius,reconnaissance_mode='hover_scan',
                  speed_mps=speed,hover_scan_seconds=hover,turn_radius_m=0.,
                  waypoint_actions=[{'index':j,'action':'hover_scan','duration_s':hover} for j in range(len(route['waypoints_m']))],
                  waypoints_wgs84=[[projection['latitude']+p[0]/projection['north_m_per_degree'],
                                     projection['longitude']-p[1]/projection['west_m_per_degree']] for p in route['waypoints_m']])
        tasks[str(i)]={'uav_id':i,'task':task}
    minx,miny,maxx,maxy=polygon.bounds
    coverage={'objective':'minimize_total_mission_time','objective_scope':'sum_of_aircraft_reconnaissance_times','uav_count':uav_count,
              'algorithm':'compact_partition_beam_search_greedy_disk_cover_open_route_2opt',
              'global_optimum_proven':False,'evaluated_partitions':len(candidates),'partition_heading_deg':heading,
              'total_mission_time_s':cost[0],'maximum_completion_time_s':cost[1],
              'maximum_aircraft_distance_m':max(r['route_distance_m'] for r in routes),
              'total_distance_m':sum(r['route_distance_m'] for r in routes),
              'total_scan_count':sum(r['scan_count'] for r in routes),
              'area_m2':polygon.area,'required_area_m2':required.area,'excluded_area_m2':terrain['excluded_area_m2'],
              'coverage_ratio':1.,'uncovered_area_m2':sum(r['uncovered_area_m2'] for r in routes),
              'reconnaissance_radius_m':radius,'speed_mps':speed,'hover_scan_seconds':hover,
              'max_region_aspect_ratio':max_aspect,'actual_max_aspect_ratio':max(shape_metrics(p)['aspect_ratio'] for p in parts),
              'internal_boundary_length_m':(sum(p.length for p in parts)-polygon.length)/2,
              'projection':projection,'coverage_margin_m':.02,'includes_transit':False,
              'tracking_note':'紧凑分区只能减少边界暴露；跨区目标去重和跟踪接管需另行实现'}
    return {'preview_only':True,'subject':'subject1','mission_id':'coverage-preview','controller_mode':'preview',
            'planned_uavs':tasks,'uavs':{},'search_area':{'coordinate_mode':'gps','coordinate_frame':'WGS84',
            'flight_profile':'competition','points':preset['points'],'points_m':points,
            'origin_x_m':minx,'origin_y_m':miny,'width_m':maxy-miny,'height_m':maxx-minx,
            'turn_radius_m':0.,'coverage':coverage,'terrain':terrain,'terrain_layers_m':layers}}


def source_digest():
    root=Path(__file__).parent
    return hashlib.sha256(b''.join((root/name).read_bytes() for name in
                         ('polygon_coverage.py','competition_area.json','competition_landcover.json'))).hexdigest()


class PlanNotPreparedError(RuntimeError):
    """比赛运行时仅允许读取已生成且来源匹配的方案。"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _plan_hash(plan):
    return hashlib.sha256(_canonical(plan).encode('utf-8')).hexdigest()


def _snapshot_path(parameters):
    if parameters == dict(radius=75., speed=5., hover=10., aspect=2., edge=5., terrain=True, uav_count=6):
        return Path(__file__).with_name('competition_coverage_default.json')
    key=hashlib.sha256(_canonical(parameters).encode('utf-8')).hexdigest()[:24]
    return Path(__file__).parent/'coverage_plans'/f'{key}.json'


@lru_cache(maxsize=16)
def _read_snapshot(path, modified_ns, size):
    # 每个文件版本只解析和校验一次；返回前深拷贝，避免请求间共享修改。
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or _plan_hash(data['plan']) != data.get('plan_sha256'):
        raise PlanNotPreparedError('固定方案文件校验失败，请在赛前重新生成')
    return data


def save_prepared_plan(plan):
    parameters=plan['prepared_plan']['parameters']
    path=_snapshot_path(parameters)
    path.parent.mkdir(parents=True,exist_ok=True)
    data={'schema_version':1,'source_digest':source_digest(),'parameters':parameters,
          'plan_sha256':_plan_hash(plan),'plan':plan}
    temporary=path.with_suffix('.tmp')
    temporary.write_text(_canonical(data),encoding='utf-8')
    temporary.replace(path)
    _read_snapshot.cache_clear()
    return path


def plan_competition_coverage(radius=75.,speed_mps=5.,hover_seconds=10.,rebuild=False,
                              max_region_aspect_ratio=2.,forest_edge_m=5.,terrain_exclusions_enabled=True,
                              uav_count=6):
    radius,speed,hover,aspect,edge=map(float,(radius,speed_mps,hover_seconds,max_region_aspect_ratio,forest_edge_m))
    for value,low,high,label in ((radius,20,150,'侦察半径'),(speed,.1,30,'飞行速度'),
                                (hover,.1,300,'悬停扫描时间'),(aspect,1.2,3,'子区长宽比上限'),(edge,1,200,'林缘保留宽度')):
        if not math.isfinite(value) or not low<=value<=high:
            raise ValueError(f'{label}需在 {low}～{high} 之间')
    if not isinstance(terrain_exclusions_enabled,bool):
        raise ValueError('地类免侦察开关必须为布尔值')
    if type(uav_count) is not int or not 1<=uav_count<=6:
        raise ValueError('无人机数量必须是 1～6 的整数')
    parameters=dict(radius=radius,speed=speed,hover=hover,aspect=aspect,edge=edge,
                    terrain=terrain_exclusions_enabled,uav_count=uav_count)
    digest=source_digest()
    if not rebuild:
        path=_snapshot_path(parameters)
        try:
            stat=path.stat()
            saved=_read_snapshot(str(path),stat.st_mtime_ns,stat.st_size)
            if saved.get('source_digest')!=digest or saved.get('parameters')!=parameters:
                raise PlanNotPreparedError('比赛边界、地类或规划代码已更新，固定方案需在赛前重新生成')
            plan=saved['plan']
            if len(plan['planned_uavs'])!=uav_count or plan['prepared_plan']['parameters']!=parameters:
                raise PlanNotPreparedError('固定方案与无人机数量或参数不符')
            return copy.deepcopy(plan)
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise PlanNotPreparedError('没有匹配区域、无人机数量和参数的固定方案，请在赛前生成；比赛模式不会自动求解') from error
    plan=copy.deepcopy(_compute(radius,speed,hover,aspect,edge,terrain_exclusions_enabled,digest,uav_count))
    identity=hashlib.sha256((digest+_canonical(parameters)).encode('utf-8')).hexdigest()[:20]
    plan['prepared_plan']={'id':identity,'parameters':parameters,'source_digest':digest,
                           'generated_at':datetime.now(timezone.utc).isoformat(),
                           'runtime_mode':'load_prepared_only','runtime_recalculation':False,
                           'landcover_status':plan['search_area']['terrain'].get('verification_status','historical_provisional')}
    return plan


def _scale_local_point(point, scale, origin_x, origin_y):
    return [round((float(point[0]) - origin_x) * scale, 6),
            round((float(point[1]) - origin_y) * scale, 6)]


def _scale_local_geometry(value, scale, origin_x, origin_y):
    """Scale GeoJSON-like local geometry without touching metadata values."""
    if isinstance(value, dict):
        return {
            key: _scale_local_geometry(item, scale, origin_x, origin_y)
            if key in ("coordinates", "geometry") else copy.deepcopy(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if len(value) >= 2 and all(isinstance(item, (int, float)) for item in value[:2]):
            return _scale_local_point(value, scale, origin_x, origin_y) + [copy.deepcopy(item) for item in value[2:]]
        return [_scale_local_geometry(item, scale, origin_x, origin_y) for item in value]
    return copy.deepcopy(value)


def adapt_competition_plan(plan, coordinate_mode="gps", flight_profile="competition",
                           max_extent_m=3.0, lab_radius_m=1.0,
                           lab_speed_mps=0.2, lab_hover_seconds=10.0,
                           gps_origin=None, gps_origins_by_uav=None):
    """Use the saved competition shape in GPS or local XYZ/ENU coordinates.

    Laboratory modes keep the saved polygon, sector boundaries and route shape,
    then scale every local coordinate so both extents are at most the selected
    maximum. Laboratory modes can publish WGS84 points from the supplied GPS
    anchor, including the 3-metre laboratory mode.
    It is deliberately an adaptation of the prepared plan, not a new runtime
    solver, so the competition machine does not repeat the expensive search.
    """
    mode = str(coordinate_mode or "gps").strip().lower()
    profile = str(flight_profile or "competition").strip().lower()
    if mode not in ("gps", "xyz"):
        raise ValueError("coordinate_mode must be gps or xyz")
    if profile not in ("lab", "lab10", "outdoor5", "competition"):
        raise ValueError("flight_profile must be lab, lab10, outdoor5 or competition")
    if max_extent_m <= 0.0:
        raise ValueError("max_extent_m must be positive")

    result = copy.deepcopy(plan)
    source_area = result.get("search_area", {})
    local_points = source_area.get("points_m") or []
    if not local_points:
        raise ValueError("固定比赛方案缺少本地多边形")
    origin_x = min(float(point[0]) for point in local_points)
    origin_y = min(float(point[1]) for point in local_points)
    width = max(float(point[1]) for point in local_points) - origin_y
    height = max(float(point[0]) for point in local_points) - origin_x
    scale = 1.0
    scaled_profile = profile in ("lab", "lab10", "outdoor5")
    if scaled_profile:
        scale = min(float(max_extent_m) / max(width, 1e-9),
                    float(max_extent_m) / max(height, 1e-9))

    radius = float(lab_radius_m if scaled_profile else source_area.get("coverage", {}).get("reconnaissance_radius_m", 75.0))
    speed = float(lab_speed_mps if scaled_profile else source_area.get("coverage", {}).get("speed_mps", 5.0))
    hover = float(lab_hover_seconds if scaled_profile else source_area.get("coverage", {}).get("hover_scan_seconds", 10.0))

    gps_latitude = gps_longitude = None
    gps_north_m_per_degree = gps_west_m_per_degree = None
    normalized_gps_origins = {}
    if mode == "gps" and profile in ("lab", "lab10", "outdoor5"):
        raw_origins = gps_origins_by_uav if isinstance(gps_origins_by_uav, dict) else {}
        if raw_origins:
            for raw_uav_id, raw_origin in raw_origins.items():
                if not isinstance(raw_origin, dict):
                    raise ValueError("每架无人机的 GPS 参考必须包含 latitude 和 longitude")
                try:
                    ref_latitude = float(raw_origin["latitude"])
                    ref_longitude = float(raw_origin["longitude"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError("每架无人机的 GPS 参考必须包含 latitude 和 longitude") from error
                if (not math.isfinite(ref_latitude) or not math.isfinite(ref_longitude)
                        or abs(ref_latitude) > 90.0 or abs(ref_longitude) > 180.0):
                    raise ValueError("无人机 GPS 参考的纬度或经度无效")
                normalized_gps_origins[str(int(raw_uav_id))] = {
                    "latitude": ref_latitude,
                    "longitude": ref_longitude,
                    "source": raw_origin.get("source", "onboard_uav{}".format(raw_uav_id)),
                }
        elif isinstance(gps_origin, dict):
            try:
                ref_latitude = float(gps_origin["latitude"])
                ref_longitude = float(gps_origin["longitude"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("gps_origin 需要 latitude 和 longitude") from error
            if (not math.isfinite(ref_latitude) or not math.isfinite(ref_longitude)
                    or abs(ref_latitude) > 90.0 or abs(ref_longitude) > 180.0):
                raise ValueError("gps_origin 的纬度或经度无效")
            normalized_gps_origins = {
                str(uav_id): {"latitude": ref_latitude, "longitude": ref_longitude}
                for uav_id in result.get("planned_uavs", {})
            }
        else:
            raise ValueError("实验室 GPS 方案需要各架无人机的 GPS 经纬度参考")
        first_origin = next(iter(normalized_gps_origins.values()))
        gps_latitude = first_origin["latitude"]
        gps_longitude = first_origin["longitude"]
        gps_north_m_per_degree, gps_west_m_per_degree = _meters_per_degree(gps_latitude)

    def local_to_gps(point, origin=None):
        origin = origin or {"latitude": gps_latitude, "longitude": gps_longitude}
        latitude = float(origin["latitude"])
        longitude = float(origin["longitude"])
        north_m_per_degree, west_m_per_degree = _meters_per_degree(latitude)
        return [
            round(latitude + float(point[0]) / north_m_per_degree, 10),
            round(longitude - float(point[1]) / west_m_per_degree, 10),
        ]

    area = result["search_area"]
    scaled_polygon = [_scale_local_point(point, scale, origin_x, origin_y) for point in local_points]
    area["coordinate_mode"] = mode
    area["coordinate_frame"] = "WGS84" if mode == "gps" else "ENU"
    area["flight_profile"] = profile
    area["origin_x_m"] = 0.0
    area["origin_y_m"] = 0.0
    area["width_m"] = round(width * scale, 6)
    area["height_m"] = round(height * scale, 6)
    if mode == "gps" and profile in ("lab", "lab10", "outdoor5"):
        area["points"] = [local_to_gps(point) for point in scaled_polygon]
        area["gps_origin"] = copy.deepcopy(next(iter(normalized_gps_origins.values())))
        area["gps_origins_by_uav"] = copy.deepcopy(normalized_gps_origins)
    if mode == "xyz":
        area["points"] = copy.deepcopy(scaled_polygon)
        area["points_m"] = copy.deepcopy(scaled_polygon)
        area["terrain_layers_m"] = _scale_local_geometry(area.get("terrain_layers_m", {}), scale, origin_x, origin_y)
    else:
        area["points_m"] = copy.deepcopy(scaled_polygon if scale != 1.0 else local_points)

    total_distance = 0.0
    total_scan_count = 0
    total_time = 0.0
    max_time = 0.0
    for uav_id, wrapper in result.get("planned_uavs", {}).items():
        task = wrapper.get("task", wrapper)
        if mode == "xyz":
            task["coordinate_frame"] = "ENU"
            task.pop("waypoints_wgs84", None)
        elif profile == "competition":
            task["coordinate_frame"] = "LOCAL_NORTH_WEST"
        elif profile in ("lab", "lab10", "outdoor5"):
            task["coordinate_frame"] = "LOCAL_NORTH_WEST"
        for key in ("polygon_m", "waypoints_m", "scan_waypoints_m", "flight_path_m"):
            if key in task:
                task[key] = [_scale_local_point(point, scale, origin_x, origin_y) for point in task[key]]
        if "bounds_m" in task:
            task["bounds_m"] = {
                "x_min": round((float(task["bounds_m"]["x_min"]) - origin_x) * scale, 6),
                "x_max": round((float(task["bounds_m"]["x_max"]) - origin_x) * scale, 6),
                "y_min": round((float(task["bounds_m"]["y_min"]) - origin_y) * scale, 6),
                "y_max": round((float(task["bounds_m"]["y_max"]) - origin_y) * scale, 6),
            }
        if mode == "gps" and profile in ("lab", "lab10", "outdoor5"):
            origin = normalized_gps_origins.get(str(uav_id))
            if origin is None:
                raise ValueError("缺少 UAV{} 的 GPS 经纬度参考".format(uav_id))
            task["waypoints_wgs84"] = [
                local_to_gps(point, origin) for point in task.get("waypoints_m", [])
            ]
        for primitive in task.get("route_primitives", []):
            for key in ("start", "end"):
                if key in primitive:
                    primitive[key] = _scale_local_point(primitive[key], scale, origin_x, origin_y)
            if "length_m" in primitive:
                primitive["length_m"] = round(float(primitive["length_m"]) * scale, 6)
        distance = float(task.get("route_distance_m", 0.0)) * scale
        scan_count = int(task.get("scan_count", len(task.get("waypoints_m", []))))
        task["route_distance_m"] = round(distance, 6)
        task["reconnaissance_radius_m"] = radius
        task["speed_mps"] = speed
        task["hover_scan_seconds"] = hover
        task["turn_radius_m"] = 0.0
        task["travel_time_s"] = distance / speed if speed > 0 else 0.0
        task["hover_scan_time_s"] = scan_count * hover
        task["mission_time_s"] = task["travel_time_s"] + task["hover_scan_time_s"]
        task["waypoint_actions"] = [
            {"index": index, "action": "hover_scan", "duration_s": hover}
            for index in range(scan_count)
        ]
        total_distance += distance
        total_scan_count += scan_count
        total_time += task["mission_time_s"]
        max_time = max(max_time, task["mission_time_s"])

    coverage = area.get("coverage", {})
    if mode == "gps" and scaled_profile:
        coverage["projection"] = {
            "latitude": gps_latitude,
            "longitude": gps_longitude,
            "north_m_per_degree": gps_north_m_per_degree,
            "west_m_per_degree": gps_west_m_per_degree,
            "method": "WGS84_local_midlatitude",
        }
    coverage.update(
        reconnaissance_radius_m=radius,
        speed_mps=speed,
        hover_scan_seconds=hover,
        total_mission_time_s=total_time,
        maximum_completion_time_s=max_time,
        maximum_aircraft_distance_m=max(
            (float(wrapper.get("task", wrapper).get("route_distance_m", 0.0))
             for wrapper in result.get("planned_uavs", {}).values()), default=0.0
        ),
        total_distance_m=total_distance,
        total_scan_count=total_scan_count,
        area_m2=float(coverage.get("area_m2", 0.0)) * scale * scale,
        required_area_m2=float(coverage.get("required_area_m2", 0.0)) * scale * scale,
        excluded_area_m2=float(coverage.get("excluded_area_m2", 0.0)) * scale * scale,
        uncovered_area_m2=0.0,
    )
    area["coverage"] = coverage
    metadata = result.get("prepared_plan")
    if isinstance(metadata, dict):
        metadata["coordinate_mode"] = mode
        metadata["flight_profile"] = profile
        metadata["runtime_mode"] = "scaled_lab_shape" if profile in ("lab", "lab10", "outdoor5") else "prepared_local_xyz"
        metadata["runtime_recalculation"] = False
    return result
