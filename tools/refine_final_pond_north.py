"""离线写入决赛2池塘北侧补扫点；点数、首点、高度和扫描参数不变。"""
import copy
import math

from competition_backend.transit_preferences import routes_from_home

UPPER = [[-115.,275.],[-205.,285.],[-210.,378.],[-135.,365.]]
LOWER = [[-115.,275.],[-135.,325.],[-185.,275.],[-205.,285.],[-185.,335.],[-205.,385.],[-230.,378.]]


def length(points):
    return sum(math.dist(a,b) for a,b in zip(points,points[1:]))


def refine_pond_north(scenes):
    scene=scenes['final_scene_2'];plan=scene['plan'];area=plan['search_area'];geometry=scene['transit_geometry']
    task=plan['planned_uavs']['4']['task'];old=copy.deepcopy(task);projection=area['coverage']['projection']
    def gps(point):
        return [projection['latitude']+point[0]/projection['north_m_per_degree'],
                projection['longitude']-point[1]/projection['west_m_per_degree']]
    for layer,points in zip(task['layers'],(UPPER,LOWER)):
        if len(layer['waypoints_m'])!=len(points) or layer['waypoints_m'][0]!=points[0]:
            raise ValueError('池塘北侧固定修订的基础点数或首点已改变，请重新核验')
        previous={tuple(p):g for p,g in zip(layer['waypoints_m'],layer['waypoints_wgs84'])}
        layer.update(waypoints_m=copy.deepcopy(points),waypoints_wgs84=[previous.get(tuple(p),gps(p)) for p in points],
            waypoint_ids=['UAV4_%s_%03d'%(layer['layer_id'],i+1) for i in range(len(points))])
    task.update(waypoints_m=task['layers'][0]['waypoints_m'],waypoints_wgs84=task['layers'][0]['waypoints_wgs84'],
        scan_waypoints_m=task['layers'][0]['waypoints_m']+task['layers'][1]['waypoints_m'],
        fixed_plan_revision='pond-north-focus-20261018-v1')
    geometry['vehicles']['4']['waypoints_m']=task['waypoints_m']
    previous_indices={tuple(p):i+1 for i,p in enumerate(old['waypoints_m'])}
    # 只替换/重排 UAV4 的上层来源项；其余来源、接收机和起降点不变。
    for uid,item in plan['planned_uavs'].items():
        routes=item['task']['transit_routes'];vehicle=geometry['vehicles'][uid]
        previous_gps={r['waypoint_index']:r for r in routes['return_paths'] if r['source_uav_id']==4}
        previous_local={r['waypoint_index']:r for r in vehicle['fleet_return_paths_m'] if r['source_uav_id']==4}
        generated,_=routes_from_home(geometry['boundary_m'],vehicle['entry_path_m'][0],UPPER,[],
            holes=geometry['excluded_polygons_m'],clearance=5.)
        updates_gps=[];updates_local=[]
        for index,point in enumerate(UPPER):
            previous=previous_indices.get(tuple(point))
            if previous is not None:
                rg=copy.deepcopy(previous_gps[previous]);rl=copy.deepcopy(previous_local[previous])
            else:
                path=list(reversed(generated[index]))
                path_gps=[list(reversed(gps(p))) for p in path]
                path_gps[0]=list(reversed(task['waypoints_wgs84'][index]));path_gps[-1]=routes['departure']
                rg=dict(source_uav_id=4,path=path_gps);rl=dict(source_uav_id=4,path=path)
            rg['waypoint_index']=index+1;rl['waypoint_index']=index+1
            updates_gps.append(rg);updates_local.append(rl)
        key=lambda r:(r['source_uav_id'],r['waypoint_index'])
        routes['return_paths']=sorted([r for r in routes['return_paths'] if r['source_uav_id']!=4]+updates_gps,key=key)
        routes['waypoints_by_uav']['4']=[list(reversed(p)) for p in task['waypoints_wgs84']]
        vehicle['fleet_return_paths_m']=sorted([r for r in vehicle['fleet_return_paths_m'] if r['source_uav_id']!=4]+updates_local,key=key)
        vehicle['return_paths_m']=[r['path'] for r in vehicle['fleet_return_paths_m'] if r['source_uav_id']==int(uid)]
    returning=geometry['vehicles']['4']['return_paths_m'][0]
    scans=[p for points in (UPPER,LOWER) for p in points+list(reversed(points))]
    task['flight_path_m']=task['entry_path_m']+scans+returning[1:]
    task['route_distance_m']=length(task['entry_path_m'])+2*(length(UPPER)+length(LOWER))+length(returning)
    task['timing_components']['horizontal_flight_seconds']=task['route_distance_m']/task['speed_mps']
    task['mission_time_s']=sum(task['timing_components'].values())
    if task['mission_time_s']>task['mission_budget_seconds']:
        raise ValueError('池塘北侧修订超过原离线16分钟预算')
    plan['prepared_plan']['pond_north_focus']=dict(revision='pond-north-focus-20261018-v1',uav_id=4,
        upper_replacement_m=dict(previous=[-185.,365.],current=[-210.,378.]),
        lower_replacement_m=dict(previous=[-145.,375.],current=[-230.,378.]),
        note='改善西南池塘北岸扫描，点数与首点不变；上下层均正逆两趟，30秒/点。')
