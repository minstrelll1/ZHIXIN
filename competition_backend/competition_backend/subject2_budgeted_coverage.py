"""科目二时间约束覆盖：锁定首航点，扫描圆避开禁扫区，联合扣除重复收益。"""
from __future__ import annotations
import copy
import itertools
import math
import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from .landing_avoidance import routes_from_home

SAMPLE_STEP_M = 5.0
MIN_NEW_DISK_FRACTION = .20
MIN_AREA_PER_SECOND = 18.0
PRIORITY_MIN_NEW_DISK_FRACTION = .15
PRIORITY_MAX_SCAN_POINTS = 3


def _grid(geometry, step):
    x0, y0, x1, y1 = geometry.bounds
    x, y = np.meshgrid(np.arange(x0 + step / 2, x1, step),
                       np.arange(y0 + step / 2, y1, step))
    points = np.column_stack((x.ravel(), y.ravel()))
    return points[contains_xy(geometry, points[:, 0], points[:, 1])]


def plan_budgeted_routes(boundary, required, safe, regions, depot, radii, max_points, speed, hover,
                         travel_weight=1.0, *, forbidden=None, first_points=None, time_limits=None,
                         minimum_new_fraction=.30, priority_regions=None):
    """联合扣除重叠；指定重点区单独计算该机覆盖收益，容许邻机重复覆盖。"""
    scan_cost = 2 * hover  # B逐点正序、逆序各扫描一次，转折点也扫描两次。
    samples = _grid(required, SAMPLE_STEP_M)
    if not len(samples):
        raise ValueError('限点规划没有有效扫描面积')
    sample_area = required.area / len(samples)
    groups = {}
    for uid, region in enumerate(regions, 1):
        radius = radii[uid]
        # 不把细窄林缘当作必须逐点抵达的扫描带；圆仍可顺带覆盖林缘。
        allowed = safe.intersection(region).intersection(required.buffer(-8.0))
        if forbidden is not None:
            allowed = allowed.difference(forbidden.buffer(radius + .05))
        points = _grid(allowed, radius * .30) if not allowed.is_empty else np.empty((0, 2))
        if first_points and uid in first_points:
            first = first_points[uid]
            if not allowed.buffer(1e-7).covers(Point(first)):
                raise ValueError('指定首航点不在允许扫描范围内')
            points = np.vstack([first, points])
        if not len(points):
            raise ValueError('子区缺少有收益的安全扫描点')
        masks = []
        priority = priority_regions.get(uid) if priority_regions else None
        priority_samples = contains_xy(priority, samples[:, 0], samples[:, 1]) if priority is not None else None
        priority_mask = (int.from_bytes(np.packbits(priority_samples, bitorder='little').tobytes(), 'little')
                         if priority_samples is not None else None)
        for point in points:
            covered = np.sum((samples-point)**2, axis=1) <= (radius-.02)**2
            masks.append(int.from_bytes(np.packbits(covered, bitorder='little').tobytes(), 'little'))
        connectors = routes_from_home(boundary, depot, points.tolist(), [], boundary_clearance=5.03)
        home_lengths = np.array([sum(math.dist(a,b) for a,b in zip(path,path[1:])) for path in connectors])
        distances = np.sqrt(np.sum((points[:, None, :]-points[None, :, :])**2, axis=2))
        groups[uid] = dict(points=points, masks=masks, home_lengths=home_lengths, connectors=connectors,
                           distances=distances, selected=[], visibility={}, priority_mask=priority_mask)

    def visible(g, first, second):
        if first is None or second is None:
            return True  # 已验证的独立进返场折线，不增加扫描点。
        key=tuple(sorted((first,second)))
        if key not in g['visibility']:
            g['visibility'][key]=safe.buffer(1e-8).covers(LineString([g['points'][first],g['points'][second]]))
        return g['visibility'][key]

    def distance(g, first, second):
        if first is None:return 2*g['home_lengths'][second]
        if second is None:return 0.
        return 2*g['distances'][first,second]

    def optimize(g):
        order=g['selected']
        for _ in range(8):
            changed=False
            for i in range(1, len(order)-1):
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
        if g['priority_mask'] is not None:return None  # 重点子区已单独选好少量分散航点。
        if len(order)>=max_points:return None
        masks = g['masks']
        gains=np.array([(mask & ~covered).bit_count()*sample_area for mask in masks])
        gains[order]=0
        minimum=math.pi*radii[uid]**2*minimum_new_fraction
        if gains.max()<minimum:return None
        chain=[None,*order,None];costs=[]
        for a,b in zip(chain,chain[1:]):
            costs.append((2*g['home_lengths'] if a is None else 2*g['distances'][a])+
                         (np.zeros(len(g['points'])) if b is None else 2*g['distances'][b])-
                         (0. if a is None and b is None else distance(g,a,b)))
        costs=np.array(costs)
        if order:
            costs[0, :] = np.inf  # 固定首航点，后续插入不可排到它之前。
        if time_limits:
            current = sum(distance(g,a,b) for a,b in zip(chain,chain[1:])) if order else 0.
            over = current/speed + len(order)*scan_cost + scan_cost + np.maximum(0,costs)/speed > time_limits[uid]
            costs[over] = np.inf
        scores=gains/(scan_cost+travel_weight*np.maximum(0,costs.min(axis=0))/speed)
        best=None
        required_rate = MIN_AREA_PER_SECOND if order else 0.
        for index in np.argsort(-scores,kind='stable'):
            if gains[index]<minimum or scores[index]<required_rate:continue
            if best and scores[index]<best[0]-1e-9:break
            for slot in np.argsort(costs[:,index],kind='stable'):
                a,b=chain[slot:slot+2]
                if not math.isfinite(costs[slot,index]):continue
                if visible(g,a,int(index)) and visible(g,int(index),b):
                    score=gains[index]/(scan_cost+travel_weight*max(0,costs[slot,index])/speed)
                    candidate=(score,gains[index],-int(index),int(index),int(slot))
                    if score>=required_rate and (best is None or candidate>best):best=candidate
                    break
        return best

    def priority_order(uid):
        # UAV3的小四边形单独比较1～3点组合，避免全局去重把重点区让给UAV5。
        # 本机任意两点至少间隔一个侦察半径，防止用高度重叠的圆补边角。
        g=groups[uid]; selected=[]; previous_gain=0
        masks=[mask & g['priority_mask'] for mask in g['masks']]
        for count in range(1,min(PRIORITY_MAX_SCAN_POINTS,max_points)+1):
            best=None
            for indices in itertools.combinations(range(len(g['points'])),count):
                if any(g['distances'][a,b]<radii[uid] for a,b in itertools.combinations(indices,2)):continue
                mask=0
                for index in indices:mask |= masks[index]
                gain=mask.bit_count()*sample_area
                if best is not None and gain<best[0]:continue
                for order in itertools.permutations(indices):
                    if first_points and uid in first_points and order[0]!=0:continue
                    if not all(visible(g,a,b) for a,b in zip(order,order[1:])):continue
                    seconds=(2*g['home_lengths'][order[0]]+2*sum(g['distances'][a,b] for a,b in zip(order,order[1:])))/speed+count*scan_cost
                    if time_limits and seconds>time_limits[uid]:continue
                    candidate=(gain,-seconds,tuple(-index for index in order),order)
                    if best is None or candidate>best:best=candidate
            if best is None:break
            if selected and best[0]-previous_gain<math.pi*radii[uid]**2*PRIORITY_MIN_NEW_DISK_FRACTION:break
            selected=list(best[3]);previous_gain=best[0]
        if not selected:raise ValueError('重点子区缺少时间预算内的安全扫描点')
        return selected

    covered=0
    # 每个子区先放置一个独立有收益的点，再联合分配剩余预算，避免邻机圆使某机空任务。
    for uid,g in groups.items():
        if g['priority_mask'] is not None:
            g['selected']=priority_order(uid)
            for index in g['selected']:covered |= g['masks'][index]
            continue
        choice = (0,0,0,0,0) if first_points and uid in first_points else pick(uid,0)
        if choice is None:raise ValueError('子区缺少初始扫描点')
        g['selected']=[choice[3]];covered |= g['masks'][choice[3]]
        if time_limits and 2*g['home_lengths'][choice[3]]/speed+scan_cost > time_limits[uid]:
            raise ValueError('首航点往返超出时间预算')
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
            if index == g['selected'][0]:continue
            others=0
            for other,h in groups.items():
                if g['priority_mask'] is not None and other != uid: continue
                for j in h['selected']:
                    if (other,j)!=(uid,index):others |= h['masks'][j]
            mask = g['masks'][index]
            if g['priority_mask'] is not None: mask &= g['priority_mask']
            gain=(mask & ~others).bit_count()*sample_area
            slot=g['selected'].index(index)
            a=g['selected'][slot-1] if slot else None
            b=g['selected'][slot+1] if slot+1<len(g['selected']) else None
            if gain<math.pi*radii[uid]**2*.10 and visible(g,a,b):g['selected'].remove(index)
        optimize(g)

    routes=[];all_disks=[]
    for uid,region in enumerate(regions,1):
        g=groups[uid];order=g['selected'];scans=g['points'][order].tolist()
        flight=g['connectors'][order[0]]+scans[1:]+scans[-2::-1]+list(reversed(g['connectors'][order[0]]))[1:]
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
            route_distance_m=line.length,travel_time_s=line.length/speed,hover_scan_time_s=len(scans)*scan_cost,
            mission_time_s=line.length/speed+len(scans)*scan_cost,required_area_m2=target.area,
            uncovered_area_m2=missing,coverage_ratio=1-missing/target.area,
            boundary_clearance_m=5.,planning_clearance_m=5.03,maximum_scan_count=max_points))
    union=unary_union(all_disks).intersection(required)
    useful_sum=sum(d.intersection(required).area for d in all_disks)
    metrics=dict(covered_area_m2=union.area,uncovered_area_m2=required.area-union.area,
                 coverage_ratio=union.area/required.area,
                 repeated_scan_area_m2=useful_sum-union.area,
                 repeated_scan_fraction=(useful_sum-union.area)/useful_sum,
                 coverage_sample_step_m=SAMPLE_STEP_M,minimum_new_disk_fraction=minimum_new_fraction,
                 minimum_area_per_second=MIN_AREA_PER_SECOND,travel_weight=travel_weight)
    return routes,metrics
