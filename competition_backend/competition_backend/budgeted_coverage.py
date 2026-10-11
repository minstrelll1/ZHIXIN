"""赛前限点覆盖：按新增有效面积/用时选择扫描点，不补齐林缘和边角。"""
from __future__ import annotations
import copy
import math
import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from .landing_avoidance import routes_from_home

SAMPLE_STEP_M = 5.0
MIN_NEW_DISK_FRACTION = .20
MIN_AREA_PER_SECOND = 18.0


def _grid(geometry, step):
    x0, y0, x1, y1 = geometry.bounds
    x, y = np.meshgrid(np.arange(x0 + step / 2, x1, step),
                       np.arange(y0 + step / 2, y1, step))
    points = np.column_stack((x.ravel(), y.ravel()))
    return points[contains_xy(geometry, points[:, 0], points[:, 1])]


def plan_budgeted_routes(boundary, required, safe, regions, depot, radii, max_points, speed, hover,
                         travel_weight=1.0):
    """六机联合选择，跨机扫描圆也扣除重叠。所有下发扫描航点直连满足安全边界。"""
    samples = _grid(required, SAMPLE_STEP_M)
    if not len(samples):
        raise ValueError('限点规划没有有效扫描面积')
    sample_area = required.area / len(samples)
    groups = {}
    for uid, region in enumerate(regions, 1):
        radius = radii[uid]
        # 不把细窄林缘当作必须逐点抵达的扫描带；圆仍可顺带覆盖林缘。
        allowed = safe.intersection(region).intersection(required.buffer(-8.0))
        points = _grid(allowed, radius * .30) if not allowed.is_empty else np.empty((0, 2))
        if not len(points):
            raise ValueError('子区缺少有收益的安全扫描点')
        masks = []
        for point in points:
            covered = np.sum((samples-point)**2, axis=1) <= (radius-.02)**2
            masks.append(int.from_bytes(np.packbits(covered, bitorder='little').tobytes(), 'little'))
        connectors = routes_from_home(boundary, depot, points.tolist(), [], boundary_clearance=5.03)
        home_lengths = np.array([sum(math.dist(a,b) for a,b in zip(path,path[1:])) for path in connectors])
        distances = np.sqrt(np.sum((points[:, None, :]-points[None, :, :])**2, axis=2))
        groups[uid] = dict(points=points, masks=masks, home_lengths=home_lengths, connectors=connectors,
                           distances=distances, selected=[], visibility={})

    def visible(g, first, second):
        if first is None or second is None:
            return True  # 已验证的独立进返场折线，不增加扫描点。
        key=tuple(sorted((first,second)))
        if key not in g['visibility']:
            g['visibility'][key]=safe.buffer(1e-8).covers(LineString([g['points'][first],g['points'][second]]))
        return g['visibility'][key]

    def distance(g, first, second):
        if first is None:return g['home_lengths'][second]
        if second is None:return g['home_lengths'][first]
        return g['distances'][first,second]

    def optimize(g):
        order=g['selected']
        for _ in range(8):
            changed=False
            for i in range(len(order)-1):
                for j in range(i+1,len(order)):
                    before=order[i-1] if i else None
                    after=order[j+1] if j+1<len(order) else None
                    old=distance(g,before,order[i])+distance(g,order[j],after)
                    new=distance(g,before,order[j])+distance(g,order[i],after)
                    if new<old-1e-7 and visible(g,before,order[j]) and visible(g,order[i],after):
                        order[i:j+1]=reversed(order[i:j+1]);changed=True
            if not changed:break

    def pick(uid, covered):
        g=groups[uid];order=g['selected']
        if len(order)>=max_points:return None
        gains=np.array([(mask & ~covered).bit_count()*sample_area for mask in g['masks']])
        gains[order]=0
        minimum=math.pi*radii[uid]**2*MIN_NEW_DISK_FRACTION
        if gains.max()<minimum:return None
        chain=[None,*order,None];costs=[]
        for a,b in zip(chain,chain[1:]):
            costs.append((g['home_lengths'] if a is None else g['distances'][a])+
                         (g['home_lengths'] if b is None else g['distances'][b])-
                         (0. if a is None and b is None else distance(g,a,b)))
        costs=np.array(costs)
        scores=gains/(hover+travel_weight*np.maximum(0,costs.min(axis=0))/speed)
        best=None
        required_rate = MIN_AREA_PER_SECOND if order else 0.
        for index in np.argsort(-scores,kind='stable'):
            if gains[index]<minimum or scores[index]<required_rate:continue
            if best and scores[index]<best[0]-1e-9:break
            for slot in np.argsort(costs[:,index],kind='stable'):
                a,b=chain[slot:slot+2]
                if visible(g,a,int(index)) and visible(g,int(index),b):
                    score=gains[index]/(hover+travel_weight*max(0,costs[slot,index])/speed)
                    candidate=(score,gains[index],-int(index),int(index),int(slot))
                    if score>=required_rate and (best is None or candidate>best):best=candidate
                    break
        return best

    covered=0
    # 每个子区先放置一个独立有收益的点，再联合分配剩余预算，避免邻机圆使某机空任务。
    for uid,g in groups.items():
        choice=pick(uid,0)
        if choice is None:raise ValueError('子区缺少初始扫描点')
        g['selected']=[choice[3]];covered |= g['masks'][choice[3]]
    for _ in range(max_points*6):
        choices=[(pick(uid,covered),uid) for uid in groups]
        choices=[(choice,uid) for choice,uid in choices if choice]
        if not choices:break
        choice,uid=max(choices,key=lambda x:(x[0][:3],-x[1]))
        index,slot=choice[3:];g=groups[uid]
        g['selected'].insert(slot,index);covered |= g['masks'][index]
        optimize(g)
    if any(not g['selected'] for g in groups.values()):
        raise ValueError('限点规划未能为六机生成有效任务')

    # 去除后来被其他扫描圆覆盖、且删去后能够安全直连的低收益点。
    for uid,g in groups.items():
        for index in list(reversed(g['selected'])):
            if len(g['selected'])<=1:break
            others=0
            for other,h in groups.items():
                for j in h['selected']:
                    if (other,j)!=(uid,index):others |= h['masks'][j]
            gain=(g['masks'][index] & ~others).bit_count()*sample_area
            slot=g['selected'].index(index)
            a=g['selected'][slot-1] if slot else None
            b=g['selected'][slot+1] if slot+1<len(g['selected']) else None
            if gain<math.pi*radii[uid]**2*.10 and visible(g,a,b):g['selected'].remove(index)
        optimize(g)

    routes=[];all_disks=[]
    for uid,region in enumerate(regions,1):
        g=groups[uid];order=g['selected'];scans=g['points'][order].tolist()
        flight=g['connectors'][order[0]]+scans[1:]+list(reversed(g['connectors'][order[-1]]))[1:]
        line=LineString(flight)
        if not safe.buffer(1e-7).covers(line) or len(scans)>max_points:
            raise ValueError('限点规划航迹或数量校验失败')
        disks=[Point(p).buffer(radii[uid]-.02,quad_segs=64) for p in scans]
        covered_region=unary_union(disks);target=required.intersection(region)
        missing=target.difference(covered_region).area
        all_disks.extend(disks)
        routes.append(dict(waypoints_m=scans,scan_waypoints_m=copy.deepcopy(scans),scan_count=len(scans),
            original_scan_count=len(scans),coverage_added_scan_count=0,turning_point_count=0,
            flight_path_m=flight,route_primitives=[dict(kind='line',start=a,end=b,length_m=math.dist(a,b)) for a,b in zip(flight,flight[1:])],
            waypoint_actions=[dict(index=i,action='hover_scan',duration_s=hover) for i in range(len(scans))],
            route_distance_m=line.length,travel_time_s=line.length/speed,hover_scan_time_s=len(scans)*hover,
            mission_time_s=line.length/speed+len(scans)*hover,required_area_m2=target.area,
            uncovered_area_m2=missing,coverage_ratio=1-missing/target.area,
            boundary_clearance_m=5.,planning_clearance_m=5.03,maximum_scan_count=max_points))
    union=unary_union(all_disks).intersection(required)
    useful_sum=sum(d.intersection(required).area for d in all_disks)
    metrics=dict(covered_area_m2=union.area,uncovered_area_m2=required.area-union.area,
                 coverage_ratio=union.area/required.area,
                 repeated_scan_area_m2=useful_sum-union.area,
                 repeated_scan_fraction=(useful_sum-union.area)/useful_sum,
                 coverage_sample_step_m=SAMPLE_STEP_M,minimum_new_disk_fraction=MIN_NEW_DISK_FRACTION,
                 minimum_area_per_second=MIN_AREA_PER_SECOND,travel_weight=travel_weight)
    return routes,metrics
