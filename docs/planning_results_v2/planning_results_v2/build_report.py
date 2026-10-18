#!/usr/bin/env python3
"""Build v2 reports, differences and fixed-plan sensitivities from final JSON.
No planner imports, runtime connections or flight commands.
"""
from pathlib import Path
import csv,hashlib,json,math
from shapely.geometry import Point,Polygon,LineString,shape
from shapely.ops import transform,unary_union

ROOT=Path(__file__).resolve().parent
METRICS=('effective_area_m2','covered_area_m2','uncovered_area_m2','coverage_ratio','independent_scan_count','redundant_area_m2','redundancy_ratio')
def read(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))
def sha(name):return hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
def save(name,obj):(ROOT/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def write_csv(name,rows):
    with (ROOT/name).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
def local(g,p):
    if not g:return Polygon()
    return transform(lambda lo,la,z=None:((la-p['latitude'])*p['north_m_per_degree'],-(lo-p['longitude'])*p['west_m_per_degree']),shape(g))
def coverage(disks,target):
    union=unary_union(disks).intersection(target) if disks else Polygon()
    total=sum(d.intersection(target).area for d in disks);red=max(0.,total-union.area)
    return dict(effective_area_m2=target.area,covered_area_m2=union.area,uncovered_area_m2=target.difference(union).area,coverage_ratio=union.area/target.area if target.area else 1.,independent_scan_count=len(disks),redundant_area_m2=red,redundancy_ratio=red/total if total else 0.),union
def ascent(u):
    ss=[s for s in u['segments'] if s['phase']=='return' and s['to_nwz_m'][2]>s['from_nwz_m'][2]+1e-9]
    return sum(s['to_nwz_m'][2]-s['from_nwz_m'][2] for s in ss),sum(s['duration_s'] for s in ss)
def phase(u,kind,lid=None):return sum(s['duration_s'] for s in u['segments'] if s['phase']==kind and (lid is None or s['layer_id']==lid))

def climb_sensitivity(result,cp,rhash):
    out=dict(artifact_type='fixed_geometry_return_climb_speed_sensitivity',planning_result_sha256=rhash,nominal_speed_mps=cp['return_climb_mps'],nominal_status=cp['return_climb_status'],speeds_mps=cp['return_climb_sensitivity_mps'],reoptimized=False,is_flight_guarantee=False,method='Replace only actual return-ascent duration by height/speed. Keep all waypoints, horizontal routes, scans and other vertical actions unchanged. Do not subtract reserve from task duration.',aircraft=[],scenes=[])
    for s in result['scenes']:
        rows=[]
        for u in s['uavs']:
            height,old_climb=ascent(u);t=u['timing'];cases=[]
            for speed in cp['return_climb_sensitivity_mps']:
                climb=height/speed;delta=climb-old_climb;total=t['mission_time_s']+delta
                cases.append(dict(return_climb_mps=speed,return_climb_s=climb,mission_time_delta_s=delta,mission_time_s=total,return_s=t['return_s']+delta,remaining_s=t['mission_limit_s']-total,planning_reserve_s=t['planning_reserve_s'],time_plus_reserve_s=total+t['planning_reserve_s'],within_hard_limit=total<=t['mission_limit_s'],within_planning_target=total+t['planning_reserve_s']<=t['mission_limit_s']))
            row=dict(scene_id=s['scene_id'],uav_id=u['uav_id'],applicable=len(u['layers'])==2,ascent_height_m=height,nominal_ascent_s=old_climb,nominal_mission_time_s=t['mission_time_s'],scenarios=cases)
            rows.append(row);out['aircraft'].append(row)
        for speed in cp['return_climb_sensitivity_mps']:
            cases=[next(v for v in row['scenarios'] if v['return_climb_mps']==speed) for row in rows]
            out['scenes'].append(dict(scene_id=s['scene_id'],return_climb_mps=speed,maximum_completion_time_s=max(v['mission_time_s'] for v in cases),total_aircraft_time_s=sum(v['mission_time_s'] for v in cases),all_aircraft_within_limit=all(v['within_hard_limit'] for v in cases),all_aircraft_within_planning_target=all(v['within_planning_target'] for v in cases)))
    return out

def terrain_sensitivity(result,audit,rhash,quad):
    out=dict(artifact_type='fixed_final_waypoints_terrain_mask_sensitivity',planning_result_sha256=rhash,terrain_mask_audit_sha256=sha('terrain_mask_audit.json'),reoptimized=False,circle_quad_segs=quad,selected_primary_variant=audit['selected_primary_variant'],geometry_source='Complete forest/water/exemption/effective geometries are in terrain_mask_audit.json; all components and holes retained.',method='Recompute exact polygon disk unions against each mask. Independently check centers, strict disks, actual scan-array and interlayer direct links, plus explicit flight segments.',limitations=['Only historical exemption changes; strong exclusions, flight boundary, waypoints and time stay fixed.','Local correction is hypothetical and does not confirm historical-pixel ownership.'],scenes=[])
    eps=1e-6
    for s in result['scenes']:
        if not s['scene_id'].startswith('final_'):continue
        p=s['projection'];boundary=local(s['boundary_geometry_wgs84'],p)
        strict=unary_union([local(e['geometry_wgs84'],p) for e in s['exclusions'] if e['kind']=='scan_disk_forbidden'])
        nofly=unary_union([local(e['geometry_wgs84'],p) for e in s['exclusions'] if e['kind']=='no_fly'])
        def line_ok(a,b):
            g=Point(a[:2]) if math.dist(a[:2],b[:2])<1e-10 else LineString([a[:2],b[:2]])
            return boundary.buffer(eps).covers(g) and g.distance(boundary.boundary)>=5-eps and (nofly.is_empty or g.distance(nofly)>=5-eps)
        for variant in audit['variants']:
            mask=local(variant['coverage_exempt_geometry_wgs84'],p);effective=boundary.difference(unary_union([mask,strict,nofly]))
            layer_disks={'upper':[],'lower':[]};rows=[];bad_lines=[];line_count=0
            for u in s['uavs']:
                region=local(u['region_geometry_wgs84'],p)
                for l in u['layers']:
                    lid=l['layer_id'];r=l['reconnaissance_radius_m'];points=l['waypoints_m'];disks=[Point(q).buffer(r,quad_segs=quad) for q in points]
                    cov,_=coverage(disks,region.intersection(effective));layer_disks[lid].extend(disks);illegal=[]
                    for wid,q in zip(l['waypoint_ids'],points):
                        point=Point(q);reasons=[]
                        if not region.buffer(eps).covers(point):reasons.append('outside_own_region')
                        if not effective.buffer(eps).covers(point):reasons.append('outside_effective_area')
                        if mask.contains(point):reasons.append('center_in_exemption')
                        if not strict.is_empty and point.distance(strict)<r+eps:reasons.append('strict_disk_clearance')
                        if not line_ok(q,q):reasons.append('flight_clearance')
                        if reasons:illegal.append(dict(waypoint_id=wid,reasons=reasons))
                    for a,b in zip(points,points[1:]):
                        line_count+=1
                        if not line_ok(a,b):bad_lines.append(dict(uav_id=u['uav_id'],layer_id=lid,kind='actual_scan_array_direct_link'))
                    rows.append(dict(uav_id=u['uav_id'],layer_id=lid,scan_radius_m=r,**cov,illegal_scan_centers=illegal,all_scan_centers_legal=not illegal,mission_time_s_unchanged=u['timing']['mission_time_s']))
                if len(u['layers'])==2:
                    line_count+=1
                    if not line_ok(u['layers'][0]['waypoints_m'][-1],u['layers'][1]['waypoints_m'][0]):bad_lines.append(dict(uav_id=u['uav_id'],kind='actual_interlayer_direct_link'))
                for seg in u['segments']:
                    if seg['phase']=='scan':continue
                    line_count+=1
                    if not line_ok(seg['from_nwz_m'],seg['to_nwz_m']):bad_lines.append(dict(uav_id=u['uav_id'],segment_id=seg['segment_id'],kind='explicit_flight_segment'))
            field={};unions={}
            for lid in ('upper','lower'):field[lid],unions[lid]=coverage(layer_disks[lid],effective)
            out['scenes'].append(dict(scene_id=s['scene_id'],terrain_variant=variant['id'],selected_primary=variant['selected'],status=variant['status'],effective_area_m2=effective.area,difference_from_audit_effective_area_m2=effective.area-variant['effective_area_m2'],layers=field,uav_layers=rows,both_layers_coverage_ratio=unions['upper'].intersection(unions['lower']).area/effective.area,either_layer_coverage_ratio=unions['upper'].union(unions['lower']).area/effective.area,legality=dict(all_scan_centers_legal=all(r['all_scan_centers_legal'] for r in rows),checked_direct_and_explicit_line_count=line_count,line_violations=bad_lines,all_checked_lines_legal=not bad_lines,strong_exclusions_unchanged=True,time_and_scan_counts_unchanged=True),maximum_completion_time_s_unchanged=s['scene_metrics']['maximum_completion_time_s']))
    return out

def revision_diff(old,new,catalog,rhash):
    out=dict(artifact_type='v1_to_v2_planning_revision_comparison',old_result_sha256=sha('inputs/v1/planning_result_v1.json'),new_result_sha256=rhash,old_source_inputs_sha256=old['source_inputs_sha256'],new_source_inputs_sha256=new['source_inputs_sha256'],interpretation='Different confirmed radii, forest geometry, direct-array constraints and return policy change together; not a single-factor experiment. V1 catalog count zero means no catalog was delivered.',scene_metrics=[],scene_layers=[],aircraft=[],uav_layers=[]);flat=[]
    def row(kind,sid,uid,lid,a,b):
        delta={k:b[k]-a[k] for k in b};r=dict(scene_id=sid,v1=a,v2=b,delta=delta)
        if uid is not None:r['uav_id']=uid
        if lid is not None:r['layer_id']=lid
        out[kind].append(r)
        for k,v in delta.items():
            unit='ratio' if 'ratio' in k else 'm2' if k.endswith('_m2') else 's' if k.endswith('_s') else 'm' if k.endswith('_m') else 'count'
            flat.append(dict(record_type=kind,scene_id=sid,uav_id=uid,layer_id=lid,metric=k,v1_value=a[k],v2_value=b[k],delta=v,units=unit))
    old_scenes={s['scene_id']:s for s in old['scenes']}
    for s in new['scenes']:
        sid=s['scene_id'];prior=old_scenes[sid];m=s['scene_metrics'];pm=prior['scene_metrics']
        def sm(scene,cat):
            z=scene['scene_metrics'];return dict(effective_area_m2=z['upper']['effective_area_m2'],maximum_completion_time_s=z['maximum_completion_time_s'],total_aircraft_time_s=z['total_aircraft_time_s'],both_layers_coverage_ratio=z['both_layers_coverage_ratio'],either_layer_coverage_ratio=z['either_layer_coverage_ratio'],return_climb_total_s=sum(ascent(u)[1] for u in scene['uavs']),return_catalog_route_count=sum(len(r['return_paths']) for r in cat.values()) if cat else 0)
        row('scene_metrics',sid,None,None,sm(prior,None),sm(s,catalog['scenes'][sid]))
        for lid in ('upper','lower'):row('scene_layers',sid,None,lid,{k:pm[lid][k] for k in METRICS},{k:m[lid][k] for k in METRICS})
        old_uavs={u['uav_id']:u for u in prior['uavs']}
        for u in s['uavs']:
            uid=u['uav_id'];pu=old_uavs[uid];cat=catalog['scenes'][sid][str(uid)]
            def um(aircraft,c):
                t={k:aircraft['timing'][k] for k in ('takeoff_s','entry_s','scan_transit_s','scan_s','layer_transition_s','return_s','mission_time_s','remaining_s')}
                t.update(return_climb_s=ascent(aircraft)[1],return_altitude_m=aircraft['segments'][-1]['to_nwz_m'][2],independent_scan_count=sum(len(l['waypoint_ids']) for l in aircraft['layers']),return_catalog_route_count=len(c['return_paths']) if c else 0);return t
            row('aircraft',sid,uid,None,um(pu,None),um(u,cat))
            old_layers={l['layer_id']:l for l in pu['layers']}
            for l in u['layers']:
                lid=l['layer_id']
                def lm(aircraft,layer):
                    z={k:layer['coverage'][k] for k in METRICS};z.update(scan_radius_m=layer['reconnaissance_radius_m'],altitude_m=layer['altitude_m'],scan_visits=len(layer['execution_order']),scan_s=phase(aircraft,'scan',layer['layer_id']),scan_transit_s=phase(aircraft,'scan_transit',layer['layer_id']),aircraft_mission_time_s=aircraft['timing']['mission_time_s']);return z
                row('uav_layers',sid,uid,lid,lm(pu,old_layers[lid]),lm(u,l))
    return out,flat

def status(name,rhash,key):
    if not (ROOT/name).exists():return dict(file=name,available=False,matches_current_result=False,passed_for_current_result=False,total_checks=None,failure_count=None)
    d=read(name);matches=d.get(key)==rhash
    return dict(file=name,available=True,matches_current_result=matches,passed_for_current_result=bool(matches and d.get('passed')),total_checks=d.get('total_checks'),failure_count=d.get('failure_count',len(d.get('failures',[]))))

def main():
    result=read('planning_result.json');old=read('inputs/v1/planning_result_v1.json');inp=read('inputs/02_规划输入_v2.json');cp=inp['common_parameters'];rhash=sha('planning_result.json')
    assert result['source_inputs_sha256']==sha('inputs/02_规划输入_v2.json'),'Final result does not match frozen input'
    catalog=read('return_catalog.json');mapping=read('topic_mapping.json')
    assert catalog['planning_result_sha256']==rhash and mapping['planning_result_sha256']==rhash,'Catalog/mapping must match final result'
    assert catalog['return_selections_sha256']==sha('return_selections.json')
    audit=read('terrain_mask_audit.json');exc=read('exclusions_draft.json');bounds=read('feasibility_bounds.json');comparison=read('candidate_comparison.json');pruning=read('redundancy_audit.json');param=result['planner']['parameters']
    assert audit['merged_input_sha256']==result['source_inputs_sha256']
    climbs=climb_sensitivity(result,cp,rhash);terrain=terrain_sensitivity(result,audit,rhash,param['circle_quad_segs']);diff,flat=revision_diff(old,result,catalog,rhash)
    save('return_climb_sensitivity.json',climbs);save('terrain_sensitivity_results.json',terrain);save('revision_diff.json',diff);write_csv('revision_diff.csv',flat)
    states=dict(planning_result_sha256=rhash,offline_plan=status('validation_report.json',rhash,'result_sha256'),offline_catalog_and_mapping=status('return_catalog_validation_report.json',rhash,'planning_result_sha256'),runtime_integration_tested=False,real_flight_validated=False);save('report_status.json',states)
    lines=[]
    def add(s=''):lines.append(s)
    add('六无人机三场景 v2 离线规划交接报告 / '+result['plan_id'])
    add('最终planning_result.json SHA256='+rhash)
    add('唯一合并输入inputs/02_规划输入_v2.json SHA256='+result['source_inputs_sha256'])
    add('旧输入/结果完整保存在inputs/v1，修订包在inputs/revision；merge_inputs.py按来源哈希合并。旧source_files只作来源记录，复算不依赖原项目绝对路径。所有指标由最终JSON或注明的对应审计生成。')
    add('工作仅限离线规划、数据、独立验证和预览；没有修改、连接、部署竞赛程序/B或发送飞行指令。')
    add('\n一、分开理解三类状态')
    for key,title in [('offline_plan','几何/名义参数/实际扫描数组直连'),('offline_catalog_and_mapping','返航目录/实际话题映射的离线核验')]:
        st=states[key];add('%s：当前结果对应=%s，通过=%s，检查数=%s，失败数=%s；见%s。'%(title,st['matches_current_result'],st['passed_for_current_result'],st['total_checks'],st['failure_count'],st['file']))
    add('当前实际话题名称已知，映射和几何通过不等于实际地面端/机载端/B已导入执行。运行时集成测试=False，实飞验证=False。决赛仍依赖池塘及树林推导连接确认；双层回升速度仍为规划暂估。')
    add('\n二、参数、执行与继承范围')
    add('上层半径%gm、下层%gm；水平%gm/s；扫描%gs/次；起飞垂直%gm/s；每机硬限%gs，按输入建议预留%gs。'%(cp['upper_scan_radius_m'],cp['lower_scan_radius_m'],cp['horizontal_speed_mps'],cp['scan_seconds'],cp['takeoff_vertical_speed_mps'],cp['mission_limit_s'],cp['preferred_reserve_s']))
    add('A：UAV1/2/3上组47/50/53m、UAV4/5/6下组34/37/40m，各机固定一层，P1…PN,PN…P1；PN连续两次各50s。最终从P1按自己的固定层高度返航，下组不额外回升。两组三分区分别覆盖全场，水平允许重叠。')
    add('B/C：UAV1/4为47→34m，2/5为50→37m，3/6为53→40m；upper一次→lower一次，同一子区各层独立覆盖。小半径层现在是lower，点位、点数、顺序已重优化。')
    add('下降%gm/s为用户接受暂定；返航回升%gm/s状态=%s，非用户确认/实测，不从起飞速度推断。'%(cp['interlayer_descent_mps'],cp['return_climb_mps'],cp['return_climb_status']))
    add('双层换层：上层末点原位下降后水平直连下层首点。结束：下层末点原位回升至本机上层，合法直连接入选定上层航点，再沿其返航目录回到名义降落点上方。不含最终落地下降。')
    add('A：T=H/v起飞+(E+2L+R)/v水平+2N*t扫描。B/C：T=Hu/v起飞+(E+Lu+D换层+Ll+D返航接入+R上层返航)/v水平+(Hu-Hl)/v下降+(Hu-Hl)/v回升+(Nu+Nl)*t扫描。')
    add('return_s完整计入回升+接入+目录返航；segments按真实时序展开。先计任务时间，再算remaining_s与30s建议余量，不把新增回升吞进余量，也不反复加减余量。')
    add('固定WGS84投影X北/Y西/Z上；waypoints_wgs84=[纬度,经度]，GeoJSON/B=[经度,纬度,相对高度]。GeoJSON Z不是海拔。各机实际开机GPS未知，共同reference_home仅是固定名义参考，不随发布者移动。')
    add('决赛采用原表前10点原序，取消旧专用通道、投影交叠和落点2.5m避让；不继承旧编号禁扫、building首点、特定机号分区/北补扫。保守保留全外界5m中心线净距。')
    add('许昌继承当前六区、机号和旧进返场中间门点/通道/辅助点，再连接新首末扫描点；六点禁飞区不得侦察/飞越且中心与全线段≥5m。没有再次执行旧交换，也不继承旧75点/10s/45m参数，不改UAV5大区。')
    add('历史森林/水体覆盖豁免允许圆盘触及及飞越但不设扫描中心；新树林和池塘是完整扫描圆不得侵入的强禁扫，不自动禁飞。共用分界线无新增5m带，扫描圆可越相邻子区。Polygon洞/MultiPolygon完整保留，polygon_m=null时旧单环接口需扩展。')
    add('\n三、树林替换与历史mask审计')
    forest=next(e for e in exc['exclusions'] if e['id']=='southeast_exclusion_anchor');pond=next(e for e in exc['exclusions'] if e['id']=='pond_exclusion_anchor');f=forest['replacement_metrics'];q=forest['derived_connectors']
    add('用户8点原值保留，原8/9/10编号来自all_source_points.point_id；原8→9南界、9→10东界，不能用Shapely环序号代替。闭合P1…P8→Q南→原9→Q东→P1。')
    add('Q南[纬度,经度]=%s；Q东=%s；P1距东界%.6fm、P8距南界%.6fm。Q点按原米制投影正交推导，非用户实测。'%(q['q_south_wgs84_lat_lon'],q['q_east_wgs84_lat_lon'],q['p1_to_east_edge_m'],q['p8_to_south_edge_m']))
    add('旧候选%.6f㎡→新树林%.6f㎡，新范围在旧范围外%.6f㎡；整体替换，非旧新并集、缩放、缓冲或凸包。'%(f['old_area_m2'],f['new_area_m2'],f['new_area_outside_old_m2']))
    add('树林来源日期%s，用户点+推导；池塘%.6f㎡、日期%s，原几何完整保留且未获本轮确认。'%(forest['source_date'],pond['area_m2'],pond['source_date']))
    add('2021 WorldCover分类约10m，2017底图均非现状测绘。原森林内缩5m/水体内缩10m已处理，不重复全场内缩。旧候选内/新树林外仍有%.6f㎡森林豁免，其同片树林归属未确认，主方案保留旧mask。'%audit['stale_exemption_candidate_area_m2'])
    for v in audit['variants']:add('%s：选用=%s，豁免%.6f㎡，有效%.6f㎡。'%(v['id'],v['selected'],v['coverage_exempt_area_m2'],v['effective_area_m2']))
    add('局部修正仅为待确认敏感性，完整保留其他森林/水体。完整替代几何在terrain_mask_audit.json；exclusions_review.png、terrain_mask_review.png和exclusions_notes.txt提供来源叠图及推导。')
    add('\n四、优化前面积上界和时间下界')
    add('feasibility_bounds在求解前计算。理想面积/时间界忽略所有水平飞行、重叠和外溢，仅是必要条件。A分机下界基于有效面积均衡候选分区，不能当作最终其他分区的面积。')
    for s in bounds['scenes']:
        add('%s 有效面积%.6f㎡。'%(s['scene_id'],s['effective_area_m2']))
        for b in s['bounds']:
            if 'uav_id' not in b:add('  %s；有效覆盖粗上界%.6f%%。'%(b['description'],100*b['ideal_coverage_ratio_ceiling']))
            elif 'minimum_independent_points_for_full_area' in b:add('  UAV%d/%s全覆盖至少%d独立点，扫描+起飞下界%.6fs，未被此下界排除=%s。'%(b['uav_id'],b['layer_id'],b['minimum_independent_points_for_full_area'],b['full_coverage_time_lower_bound_s'],b['not_ruled_out_by_time_lower_bound']))
            else:add('  UAV%d至少upper%d/lower%d点；扫描+起飞+下降+回升下界%.6fs，未被此下界排除=%s。'%(b['uav_id'],b['minimum_upper_points_for_full_area'],b['minimum_lower_points_for_full_area'],b['full_coverage_time_lower_bound_s'],b['not_ruled_out_by_time_lower_bound']))
    x5=next(b for s in bounds['scenes'] if s['scene_id']=='xuchang_small_2' for b in s['bounds'] if b.get('uav_id')==5)
    add('许昌UAV5面积%.6f㎡，上%d/下%d点，仅扫描%.3fs，计全部垂直动作下界%.6fs；固定分区双层100%%不可能，必须保留覆盖缺口。'%(x5['effective_area_m2'],x5['minimum_upper_points_for_full_area'],x5['minimum_lower_points_for_full_area'],(x5['minimum_upper_points_for_full_area']+x5['minimum_lower_points_for_full_area'])*cp['scan_seconds'],x5['full_coverage_time_lower_bound_s']))
    add('\n五、算法、直连与逐次删除冗余点')
    add('算法%s/%s，固定种子%d为版本约定；无随机抽样。圆心候选%gm格点+每15m合法边缘点；目标采样%gm仅用于启发式，最终布尔计算每四分圆%d段。'%(result['planner']['name'],result['planner']['version'],result['planner']['random_seed'],param['candidate_grid_step_m'],param['target_sample_step_m'],param['circle_quad_segs']))
    add('生成外界净距%gm、强禁扫圆心半径+%gm；禁飞圆角5.06m/象限8段最小弦距>5.03m。可见性图绕障仅用于有完整路径载荷的进场/返航。'%(param['clearance_generation_m'],param['disk_clearance_extra_m']))
    add('每层每轮最多%d个高收益候选，试全部插入位置。收益为新增面积比例/新增秒数^%g；双层另除以(%g+该层当前采样覆盖比例)，避免大半径层挤占小半径层。'%(param['greedy_shortlist_per_layer'],param['marginal_time_exponent'],param['layer_balance_offset']))
    add('双层初始化各24个高收益候选配对；2-opt最多8轮。所有扫描数组相邻点及换层必须直接连接合法；不依赖未下发的绕行辅助点。换层考察8对近邻，目标总路长+3*max(换层水平-20,0)，20m为软目标。')
    add('正常返航在本机上层点中选可直接接入且接入+返航总长最短者，记录来源机/点序；全目录仍含六机所有上层点。单次贪心最多40轮，没有达到max(2样本,%.3f%%)且满足预算的短名单候选即停；未宣称全局最优。'%(100*param['minimum_sample_gain_ratio']))
    add('两种确定性决赛分区为有效面积均衡/总面积均衡；A北1区南2区，B北中南三带各2区。按两层全场覆盖率和最大、再最短最慢时间选分区；许昌不改区。')
    add('最终逐点重算本机层/全场同层独占贡献；两者均<%.3f㎡才试删点、重排及完整时间检查，且需非空、直连合法、预算内并缩时。每接受一删立即重算，不批删“分别可删”的集合。再利用释放预算最多3轮贪心补点、再删；保留转弯扫描点须支付实际扫描时间并说明。'%param['redundancy_deletion_threshold_m2'])
    for s in result['scenes']:
        a=pruning['scenes'][s['scene_id']];zero=[v for v in a['final_points'] if v['zero_contribution_both']]
        add('%s：初始删前总机时%.6fs→最终删/补后%.6fs；删%d点，补%d点，最终双口径零贡献%d点。'%(s['scene_id'],sum(v['time_s'] for v in a['before']),sum(v['time_s'] for v in a['after']),len(a['removed']),sum(v['added_points'] for v in a.get('refill_rounds',[])),len(zero)))
        for i,v in enumerate(a['removed'],1):add('  删除事件%d UAV%d/%s candidate%d：独占本机/全场%.9f/%.9f㎡，时间%.6f→%.6fs，纯扫描节省%.3fs。'%(i,v['uav_id'],v['layer_id'],v['candidate_index'],v['own_exclusive_area_m2'],v['field_exclusive_area_m2'],v['before_time_s'],v['after_time_s'],v['scan_time_saved_s']))
        for v in a.get('refill_rounds',[]):add('  补点轮%d：增%d点，再删%d点；该轮删前/删后总机时%.6f/%.6fs。'%(v['round'],v['added_points'],v['deleted_points'],sum(q['time_s'] for q in v['before_pruning']),sum(q['time_s'] for q in v['after_pruning'])))
        for v in zero:add('  保留%s：%s'%(v['waypoint_id'],v.get('retention_reason','见审计')))
    add('\n六、两个完整候选')
    add('候选 / 场景 / 分区 / 选中分区 / 预算s / 上% / 下% / 最慢s / 总机时s')
    for c in comparison:add('%s / %s / %s / %s / %.0f / %.6f / %.6f / %.6f / %.6f'%(c['candidate'],c['scene_id'],c['variant'],c['selected_partition'],c['budget_s'],100*c['upper_coverage'],100*c['lower_coverage'],c['maximum_completion_time_s'],c['total_aircraft_time_s']))
    add('主方案采用coverage_priority；more_reserve是完整独立优化候选。切换时须重建全部导出、目录、验证、报告和预览，不能混用。完整数据见candidates/。')
    for c in comparison:
        if c['selected_partition']:add('%s/%s规模：%s。'%(c['candidate'],c['scene_id'],'；'.join('UAV%d候选%s/样本%s'%(u['uav_id'],u['candidate_counts'],u['sample_counts']) for u in c['uavs'])))
    add('\n七、最终场景与每机每层汇总（长度m，面积㎡，时间s）');summary=[]
    for s in result['scenes']:
        sid=s['scene_id'];m=s['scene_metrics'];add('%s / %s / status=%s'%(s['display_name'],sid,s['status']))
        add('有效%.6f；上层%.6f%%、下层%.6f%%；两层都覆盖%.6f%%，至少一层%.6f%%；最慢%.6fs（%.6fmin），总机时%.6fs，全部≤%gs=%s。'%(m['upper']['effective_area_m2'],100*m['upper']['coverage_ratio'],100*m['lower']['coverage_ratio'],100*m['both_layers_coverage_ratio'],100*m['either_layer_coverage_ratio'],m['maximum_completion_time_s'],m['maximum_completion_time_s']/60,m['total_aircraft_time_s'],cp['mission_limit_s'],m['all_aircraft_within_limit']))
        for lid in ('upper','lower'):
            v=m[lid];add('  全场%s：独立点%d，覆盖%.6f，缺口%.6f，冗余%.6f/%.6f%%。'%(lid,v['independent_scan_count'],v['covered_area_m2'],v['uncovered_area_m2'],v['redundant_area_m2'],100*v['redundancy_ratio']))
        add('  UAV / 点数 / 起飞 / 进场 / 扫描移动 / 扫描 / 换层 / 回升 / 接入m / 目录返航m / return总s / 总s / 剩余s / 总+建议余量s')
        for u in s['uavs']:
            uid=u['uav_id'];t=u['timing'];cat=catalog['scenes'][sid][str(uid)];ret=cat['normal_return_connection'];counts=','.join(l['layer_id']+':'+str(len(l['waypoint_ids'])) for l in u['layers'])
            add('  %d / %s / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f / %.6f'%(uid,counts,t['takeoff_s'],t['entry_s'],t['scan_transit_s'],t['scan_s'],t['layer_transition_s'],ascent(u)[1],ret['connection_path']['horizontal_distance_m'],ret['return_route_distance_m'],t['return_s'],t['mission_time_s'],t['remaining_s'],t['budget_with_reserve_s']))
            add('    返航接入来源UAV%d/点%d/%s；返回高度%.3fm。'%(ret['source_uav_id'],ret['waypoint_index'],ret['waypoint_id'],cat['recipient_return_altitude_m']))
            for l in u['layers']:
                v=l['coverage'];add('    %s 高%.3fm/R%.3fm，%d点×%d遍：有效%.6f，覆盖%.6f/%.6f%%，缺口%.6f，冗余%.6f/%.6f%%；扫描%.6fs，层内移动%.6fs。'%(l['layer_id'],l['altitude_m'],l['reconnaissance_radius_m'],len(l['waypoint_ids']),l['scan_passes'],v['effective_area_m2'],v['covered_area_m2'],100*v['coverage_ratio'],v['uncovered_area_m2'],v['redundant_area_m2'],100*v['redundancy_ratio'],phase(u,'scan',l['layer_id']),phase(u,'scan_transit',l['layer_id'])))
                summary.append(dict(scene_id=sid,uav_id=uid,layer_id=l['layer_id'],point_count=len(l['waypoint_ids']),scan_radius_m=l['reconnaissance_radius_m'],**{k:v[k] for k in METRICS},scan_s=phase(u,'scan',l['layer_id']),scan_transit_s=phase(u,'scan_transit',l['layer_id']),mission_time_s=t['mission_time_s'],return_climb_s=ascent(u)[1],remaining_s=t['remaining_s']))
            if u['layer_transition']:
                v=u['layer_transition'];add('    换层直连%.6fm，下降%.3fm，合计%.6fs；20m软目标=%s；%s'%(v['horizontal_distance_m'],v['vertical_distance_m'],v['duration_s'],v['preferred_limit_met'],v['note']))
        for c in m['partition_checks']:add('  分区%s：重叠%.9f㎡/缝隙%.9f㎡/越界%.9f㎡，全部有效=%s。'%(c['group'],c['overlap_area_m2'],c['gap_area_m2'],c['outside_area_m2'],c['all_regions_valid']))
    write_csv('mission_summary.csv',summary)
    add('\n八、返航目录与当前话题适配')
    add('已知/ground_mission_planner/vehicle_N/path_stage_1、path_stage_2为Float64MultiArray，以[经度,纬度,相对高度]展平；return_paths为JSON String。精确逐机话题、实际数组与顺序见topic_mapping.json，未发布这些消息。')
    add('B/C：stage1=upper、stage2=lower各一趟；运行return_paths schema_version=2、route_scope=all_uav_waypoints、waypoint_layer=upper、return_policy=climb_to_upper_then_return。每接收机均含六机上层所有唯一点，索引从1开始，全部路线使用接收机上层高度。')
    add('A：唯一物理层均映射path_stage_1，包括UAV4/5/6的lower；path_stage_2为空，旧单层方式正逆两趟。task.layers当前仅支持真正upper+lower双层，不能塞入A单层数组。A完整固定层目录仍需legacy适配，不假装当前双层目录能直接导入。')
    add('主schema保持version1及ros_topics=null、proposed_only=true；已知话题放独立映射文件。目录有独立schema，其离线artifact版本与运行话题JSON版本分开。未编造mission_id、assignment_checksum或实测GPS。')
    for s in result['scenes']:
        recipients=catalog['scenes'][s['scene_id']];add('%s目录总%d条。'%(s['scene_id'],sum(len(r['return_paths']) for r in recipients.values())))
        for uid in sorted(recipients,key=int):
            r=recipients[uid];add('  接收UAV%s：%d条、来源层%s、返航高%.3fm；六机来源点数%s。'%(uid,len(r['return_paths']),r['waypoint_layer'],r['recipient_return_altitude_m'],r['waypoint_counts_by_uav']))
    add('每个扫描数组点均实际扫描，没有只在segments存在而数组漏发的扫描辅助拐点。进场与返航目录含完整不扫描路径点，仍需后续适配器传送相应完整载荷；尚无运行时导入测试。')
    add('\n九、回升敏感性：固定全部最终几何，不重优化')
    add('只替换真实回升段时间为高度/速度，其他时间不变。A无回升，三种速度相同；余量不吞回升。完整JSON见return_climb_sensitivity.json。')
    for row in climbs['aircraft']:add('%s UAV%d回升%.3fm：%s。'%(row['scene_id'],row['uav_id'],row['ascent_height_m'],'；'.join('%gm/s 回升%.6fs/任务%.6fs/剩余%.6fs/硬限%s/建议余量%s'%(v['return_climb_mps'],v['return_climb_s'],v['mission_time_s'],v['remaining_s'],v['within_hard_limit'],v['within_planning_target']) for v in row['scenarios'])))
    for row in climbs['scenes']:add('  %s/%gm/s最慢%.6fs，总机时%.6fs，全部硬限=%s，全部建议余量=%s。'%(row['scene_id'],row['return_climb_mps'],row['maximum_completion_time_s'],row['total_aircraft_time_s'],row['all_aircraft_within_limit'],row['all_aircraft_within_planning_target']))
    add('\n十、历史mask固定点位敏感性：非重新优化结果')
    for row in terrain['scenes']:add('%s/%s：有效%.6f㎡；上%.6f%%/冗余%.6f㎡、下%.6f%%/冗余%.6f㎡；两层都覆盖%.6f%%；中心合法=%s、直连及显式全航段合法=%s。'%(row['scene_id'],row['terrain_variant'],row['effective_area_m2'],100*row['layers']['upper']['coverage_ratio'],row['layers']['upper']['redundant_area_m2'],100*row['layers']['lower']['coverage_ratio'],row['layers']['lower']['redundant_area_m2'],100*row['both_layers_coverage_ratio'],row['legality']['all_scan_centers_legal'],row['legality']['all_checked_lines_legal']))
    add('每机层面积、覆盖、冗余、缺口与合法性列表见terrain_sensitivity_results.json。新增目标面积可使覆盖百分比下降；不代表修改mask后重新优化的最优收益，也不改变强禁扫边界。')
    add('\n十一、v1→v2逐项差异')
    add('revision_diff.json/CSV记录3场景、6个全场层、18机、30个机层；半径和有效面积分母、直连和返航政策同时修改，覆盖率差不能全部归因于算法优劣，也不能当单因素因果试验。v1目录0表示旧包未提供目录。')
    for d in diff['scene_metrics']:
        a,b=d['v1'],d['v2'];add('%s：有效面积%.6f→%.6f㎡，最慢%.6f→%.6fs，总机时%.6f→%.6fs，回升%.6f→%.6fs，目录%d→%d条。'%(d['scene_id'],a['effective_area_m2'],b['effective_area_m2'],a['maximum_completion_time_s'],b['maximum_completion_time_s'],a['total_aircraft_time_s'],b['total_aircraft_time_s'],a['return_climb_total_s'],b['return_climb_total_s'],a['return_catalog_route_count'],b['return_catalog_route_count']))
    for d in diff['scene_layers']:
        a,b=d['v1'],d['v2'];add('  %s/%s点%d→%d，覆盖%.6f→%.6f%%，冗余%.6f→%.6f㎡/%.6f→%.6f%%。'%(d['scene_id'],d['layer_id'],a['independent_scan_count'],b['independent_scan_count'],100*a['coverage_ratio'],100*b['coverage_ratio'],a['redundant_area_m2'],b['redundant_area_m2'],100*a['redundancy_ratio'],100*b['redundancy_ratio']))
    for d in diff['uav_layers']:
        a,b=d['v1'],d['v2'];add('  %s UAV%d/%s R%.0f→%.0fm，点%d→%d，覆盖%.6f→%.6f㎡(%.4f→%.4f%%)，冗余%.6f→%.6f㎡，机总时%.6f→%.6fs。'%(d['scene_id'],d['uav_id'],d['layer_id'],a['scan_radius_m'],b['scan_radius_m'],a['independent_scan_count'],b['independent_scan_count'],a['covered_area_m2'],b['covered_area_m2'],100*a['coverage_ratio'],100*b['coverage_ratio'],a['redundant_area_m2'],b['redundant_area_m2'],a['aircraft_mission_time_s'],b['aircraft_mission_time_s']))
    add('\n十二、覆盖口径、独立验证和复现')
    add('本机层分母=本机区∩有效目标，全场层分母=有效目标；分子为独立扫描圆并集∩分母。A正逆不增加几何覆盖，上下层不相加冒充一层100%。交集“都覆盖”与并集“至少一层”分别报告。')
    add('同层冗余=各独立圆裁剪面积和−并集面积，冗余率除以该面积和；跨层重复是任务要求。主JSON/GeoJSON保留完整未覆盖几何。')
    add('validate_plan.py不导入planner，独立校核schema、来源、坐标、分区、覆盖、强禁扫、所有航段、实际扫描数组直连、回升/接入与时间账、CSV/GeoJSON一致性。数值容差坐标1e-4m、面积0.01㎡、时间账0.1s不实质放宽5m和1380s硬限。')
    add('validate_return_catalog.py另核验目录数量、索引、接收机高度、路径起终点/合法性及话题映射。test_validator_tamper.py有半径、树林、回升、漏发辅助点等负例；见validator_tamper_report.json。schema通过不是几何/时间通过，更不是实飞保证。')
    add('复现：python3 merge_inputs.py；python3 planner.py；python3 build_return_catalog.py；python3 validate_plan.py；python3 validate_return_catalog.py；python3 test_validator_tamper.py；python3 build_report.py；python3 build_exclusions_review_v2.py；python3 build_preview.py。依赖见requirements.txt；所有文件路径相对交付根目录。')
    add('主schema仍在inputs/v1/03_规划输出.schema.json，回升使用已有phase=return且return_s完整计入。目录另有schema；已知话题不写入旧schema的null字段。最终打包生成输入/输出SHA256清单，source_inputs_sha256指向当前合并输入。')
    for name,title,key in [('input_reproducibility_report.json','纯原始输入独立重合并','identical'),('reproducibility_report.json','隔离目录全量重优化及目录/预览重建','byte_identical')]:
        if (ROOT/name).exists():
            rr=read(name);checks=rr['checks'];add('%s：%d/%d文件字节一致，通过=%s；实际执行方法与每文件哈希见%s。'%(title,sum(bool(c.get(key)) for c in checks),len(checks),rr['passed'],name))
    add('\n十三、待确认事项与交回原程序开发对话的文件')
    add('仍待确认：池塘真实边界，树林Q补点连接，历史同片树林归属，返航回升速度，各机实际开机GPS/高度基准，暂定高度与下降能力。未实测风、加减速、耗电、等待/目标跟踪、通信失联及多机动态避碰；30s建议余量不保证真实时限。')
    add('建议交回整个planning_results_v2.zip：新输入、主JSON/GeoJSON/CSV、planner/config、所有验证报告、return_catalog/schema、topic_mapping、revision_diff、敏感性/来源审查、独立预览和依赖复现说明。')
    add('后续仍需scene1单层stage1适配、双层返航接入策略、每机真实GPS替换、多环几何/完整进返场路径导入、运行时任务标识与安全核验；本轮未修改任何运行程序。')
    (ROOT/'planning_report.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(planning_result_sha256=rhash,report_lines=len(lines),scene_differences=len(diff['scene_metrics']),aircraft_differences=len(diff['aircraft']),layer_differences=len(diff['uav_layers']),terrain_cases=len(terrain['scenes']),statuses=states),ensure_ascii=False))

if __name__=='__main__':main()
