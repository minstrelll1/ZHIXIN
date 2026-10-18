#!/usr/bin/env python3
"""Deterministically merge immutable v1 inputs and the supplied v2 revision.

This creates planning inputs, never flight commands. All paths are relative to
this script. The primary terrain exemption remains unchanged because the
ownership of historical forest pixels has not been confirmed.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from shapely.geometry import Point, Polygon, shape, mapping
from shapely.ops import transform, unary_union

ROOT=Path(__file__).resolve().parent
OLD=ROOT/'inputs/v1/02_规划输入.json'
OLD_EXCLUSIONS=ROOT/'inputs/v1/exclusions_draft_v1.json'
REVISION=ROOT/'inputs/revision/修订需求.json'
NEW=ROOT/'inputs/02_规划输入_v2.json'

def read(path):return json.loads(path.read_text(encoding='utf-8'))
def save(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    old=read(OLD); old_ex=read(OLD_EXCLUSIONS); revision=read(REVISION)
    assert sha(OLD)==revision['original_inputs_sha256'],'Immutable original input hash mismatch'
    old_plan=ROOT/'inputs/v1/planning_result_v1.json'
    assert sha(old_plan)==revision['original_result_sha256'],'Immutable original plan hash mismatch'
    override=revision['confirmed_overrides']
    completion=revision['proposed_not_user_measured_boundary_completion']
    proj=old['final_field']['projection']
    assert proj==completion['projection'],'Revision projection differs from original fixed projection'
    assert old['final_field']['boundary_source_point_ids']==list(range(1,11))
    def nw(lon,lat,z=None):
        return ((lat-proj['latitude'])*proj['north_m_per_degree'],
                -(lon-proj['longitude'])*proj['west_m_per_degree'])
    def ll(n,w,z=None):
        return (proj['longitude']-w/proj['west_m_per_degree'],
                proj['latitude']+n/proj['north_m_per_degree'])
    def metric(geometry):return transform(nw,shape(geometry))
    def geodetic(geometry):return mapping(transform(ll,geometry))
    def project(point,a,b):
        ab=[b[k]-a[k] for k in (0,1)]
        t=sum((point[k]-a[k])*ab[k] for k in (0,1))/sum(v*v for v in ab)
        assert 0<=t<=1,'Projected connector must be on its original edge segment'
        return [a[k]+t*ab[k] for k in (0,1)],t
    source_points={p['point_id']:p for p in old['final_field']['all_source_points']}
    expected={8:[33.86095,113.70356666666667],9:[33.86063055555556,113.70715],
              10:[33.862436111111116,113.70729166666666]}
    for number,latlon in expected.items():
        assert [source_points[number]['latitude'],source_points[number]['longitude']]==latlon
        assert old['final_field']['boundary_wgs84_lat_lon'][number-1]==latlon
    original_nw={i:nw(p['longitude'],p['latitude']) for i,p in source_points.items()}
    chain=override['southeast_forest_user_chain_lat_lon']
    assert chain==completion['chain_lat_lon'] and len(chain)==8
    chain_nw=[nw(lon,lat) for lat,lon in chain]
    qe,te=project(chain_nw[0],original_nw[9],original_nw[10])
    qs,ts=project(chain_nw[-1],original_nw[8],original_nw[9])
    q_east_lonlat=ll(*qe);q_south_lonlat=ll(*qs)
    q_east_latlon=list(q_east_lonlat)[::-1];q_south_latlon=list(q_south_lonlat)[::-1]
    connector_errors={
        'q_east_supplied_vs_computed_m':math.dist(qe,nw(*completion['q_east_lat_lon'][::-1])),
        'q_south_supplied_vs_computed_m':math.dist(qs,nw(*completion['q_south_lat_lon'][::-1]))}
    assert max(connector_errors.values())<1e-6
    # Preserve all eight user coordinates verbatim. Only Q points are derived.
    forest_ring=[[lon,lat] for lat,lon in chain]+[list(q_south_lonlat),
        [source_points[9]['longitude'],source_points[9]['latitude']],list(q_east_lonlat)]
    forest_geometry={'type':'Polygon','coordinates':[forest_ring+[forest_ring[0]]]}
    new_forest=metric(forest_geometry)
    assert new_forest.is_valid
    assert new_forest.symmetric_difference(metric(completion['geometry_wgs84'])).area<0.0001
    assert abs(new_forest.area-completion['area_m2'])<0.0001
    boundary=Polygon(old['final_field']['boundary_m'])
    assert new_forest.difference(boundary).area<0.0001
    old_by_id={e['id']:e for e in old_ex['exclusions']}
    old_forest=metric(old_by_id['southeast_exclusion_anchor']['geometry_wgs84'])
    pond_record=deepcopy(old_by_id['pond_exclusion_anchor'])
    pond=metric(pond_record['geometry_wgs84'])
    # Paths now refer to the immutable v1 folder; pond geometry is unchanged.
    pond_record.update(source='inputs/v1/05_历史底图.jpg; unchanged v1 manual trace in inputs/v1/exclusions_draft_v1.json',
                       source_date=old_ex['source_date'],geometry_status='provisional',
                       confirmation_required=True,
                       interpretation=pond_record['interpretation']+' 本次修订未确认或修改池塘边界。')
    # This optional audit was embedded in the v1 record. Recompute its layer
    # labels from the same revision overrides; never retain upper=30/lower=50.
    pond_record['sensitivity']=[]
    for delta in (-10.0,0.0,10.0):
        trial=pond.buffer(delta,quad_segs=128).intersection(boundary)
        old_terrain_mask=metric(old['final_field']['terrain']['coverage_exempt_geometry_wgs84'])
        pond_record['sensitivity'].append({
            'boundary_offset_m':delta,'area_within_boundary_m2':trial.area,
            'additional_area_removed_after_terrain_exemption_m2':trial.difference(old_terrain_mask).area,
            'upper_scan_centers_forbidden_within_boundary_m2':trial.buffer(override['upper_scan_radius_m'],quad_segs=128).intersection(boundary).area,
            'lower_scan_centers_forbidden_within_boundary_m2':trial.buffer(override['lower_scan_radius_m'],quad_segs=128).intersection(boundary).area})
    assert pond_record['geometry_wgs84']==old_by_id['pond_exclusion_anchor']['geometry_wgs84']
    anchor=next(e for e in old['final_field']['strict_scan_exclusions'] if e['id']=='southeast_exclusion_anchor')
    assert new_forest.covers(Point(*nw(anchor['longitude'],anchor['latitude'])))
    forest_record={
        'id':'southeast_exclusion_anchor','kind':'scan_disk_forbidden',
        'geometry_wgs84':forest_geometry,'geometry_status':'provisional',
        'boundary_status':'user_chain_confirmed_with_developer_proposed_connectors',
        'user_chain_status':'confirmed_in_supplied_revision',
        'connector_status':'developer_derived_not_user_measured',
        'source':'inputs/revision/修订需求.json; eight user points plus orthogonal connectors on original numbered edges',
        'source_date':revision['date'],'source_sha256':sha(REVISION),
        'anchor_wgs84_lon_lat':[anchor['longitude'],anchor['latitude']],
        'user_chain_wgs84_lat_lon':deepcopy(chain),
        'original_boundary_vertex_ids':[8,9,10],
        'closure_rule':completion['closure_rule'],
        'derived_connectors':{
            'q_south_wgs84_lat_lon':q_south_latlon,'q_east_wgs84_lat_lon':q_east_latlon,
            'q_south_nw_m':qs,'q_east_nw_m':qe,'south_edge_original_point_ids':[8,9],
            'east_edge_original_point_ids':[9,10],'south_projection_parameter':ts,
            'east_projection_parameter':te,
            'p1_to_east_edge_m':math.dist(chain_nw[0],qe),
            'p8_to_south_edge_m':math.dist(chain_nw[-1],qs),**connector_errors},
        'polygon_m':[list(p) for p in list(new_forest.exterior.coords)[:-1]],
        'area_m2':new_forest.area,'anchor_inside':True,'geometry_valid':True,
        'outside_boundary_area_m2':new_forest.difference(boundary).area,
        'flight_prohibited':False,'radius_m':None,
        'confirmation_required':True,
        'interpretation':'整体替换旧东南候选。8个用户点保持原值；Q南、Q东按原固定投影和原8/9/10编号正交推导，并非用户实测补点。强禁扫不自动禁飞。',
        'replacement_metrics':{'old_area_m2':old_forest.area,'new_area_m2':new_forest.area,
            'new_area_outside_old_m2':new_forest.difference(old_forest).area,
            'old_area_outside_new_m2':old_forest.difference(new_forest).area}}

    terrain=old['final_field']['terrain']
    raw_forest=metric(terrain['forest_geometry_wgs84'])
    raw_water=metric(terrain['water_geometry_wgs84'])
    old_mask=metric(terrain['coverage_exempt_geometry_wgs84'])
    # A sensitivity scenario removes only historical FOREST pixels in the
    # review zone. Water and every other location remain unchanged. This is
    # not selected: point membership cannot establish forest ownership.
    review_zone=old_forest.difference(new_forest)
    stale_forest=old_mask.intersection(raw_forest).intersection(review_zone).difference(raw_water)
    raw_forest_correction=raw_forest.intersection(review_zone).difference(raw_water)
    corrected_mask=old_mask.difference(stale_forest)
    corrected_forest=raw_forest.difference(raw_forest_correction)
    strict_union=unary_union([new_forest,pond])
    old_effective=boundary.difference(unary_union([old_mask,old_forest,pond]))
    main_effective=boundary.difference(unary_union([old_mask,strict_union]))
    corrected_effective=boundary.difference(unary_union([corrected_mask,strict_union]))
    assert corrected_mask.symmetric_difference(old_mask).difference(review_zone).area<1e-8
    assert corrected_mask.intersection(raw_water).symmetric_difference(old_mask.intersection(raw_water)).area<1e-8
    assert main_effective.area>old_effective.area
    source_paths=[OLD,OLD_EXCLUSIONS,old_plan,REVISION,
        ROOT/'inputs/revision/修改方案_发给规划对话.txt',
        ROOT/'inputs/revision/新树林边界_含推导补点.json',
        ROOT/'inputs/revision/当前程序B双层接口说明.txt',
        ROOT/'inputs/v1/05_历史底图.jpg']
    provenance=[{'file':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in source_paths]
    audit={
        'artifact_type':'historical_terrain_mask_local_ownership_audit',
        'version':2,'selected_primary_variant':'retain_historical_mask',
        'local_pixel_ownership_confirmed':False,
        'source_provenance':provenance,'projection':proj,
        'classification_provenance':terrain['provenance'],
        'geometry_order':'GeoJSON [longitude,latitude]; all metrics in original [north,west] metre projection',
        'method':'Overlay raw historical forest/water and preprocessed exemption with (old southeast candidate minus replacement forest). Primary retains the exact historical mask. The alternative deletes only eligible historical forest exemption in this review zone, subject to future ownership confirmation.',
        'why_primary_retains_mask':'8个用户点确认当前树林轮廓，但不足以证明旧候选内/新树林外所有2021森林像元均属于同一片树林。2017影像和2021分类也不能确认现状。因此主方案保留旧mask，局部修正仅为待确认敏感性。',
        'review_zone_geometry_wgs84':geodetic(review_zone),
        'stale_exemption_candidate_geometry_wgs84':geodetic(stale_forest),
        'raw_forest_correction_candidate_geometry_wgs84':geodetic(raw_forest_correction),
        'review_zone_area_m2':review_zone.area,
        'stale_exemption_candidate_area_m2':stale_forest.area,
        'raw_forest_correction_candidate_area_m2':raw_forest_correction.area,
        'water_in_review_zone_area_m2':raw_water.intersection(review_zone).area,
        'old_terrain_intersection_new_forest_m2':old_mask.intersection(new_forest).area,
        'all_old_exemption_in_review_zone_m2':old_mask.intersection(review_zone).area,
        'exemption_in_review_zone_not_eligible_as_forest_m2':old_mask.intersection(review_zone).difference(stale_forest).area,
        'original_old_effective_area_m2':old_effective.area,
        'old_strict_southeast_geometry_wgs84':old_by_id['southeast_exclusion_anchor']['geometry_wgs84'],
        'new_strict_southeast_geometry_wgs84':forest_geometry,
        'pond_geometry_wgs84':pond_record['geometry_wgs84'],
        'variants':[
            {'id':'retain_historical_mask','selected':True,'status':'primary_conservative_provisional',
             'forest_geometry_wgs84':terrain['forest_geometry_wgs84'],
             'water_geometry_wgs84':terrain['water_geometry_wgs84'],
             'coverage_exempt_geometry_wgs84':terrain['coverage_exempt_geometry_wgs84'],
             'effective_area_geometry_wgs84':geodetic(main_effective),
             'forest_area_m2':raw_forest.area,'water_area_m2':raw_water.area,
             'coverage_exempt_area_m2':old_mask.area,'effective_area_m2':main_effective.area,
             'removed_exemption_area_m2':0.0},
            {'id':'local_forest_correction_hypothesis','selected':False,
             'status':'sensitivity_only_requires_local_forest_ownership_confirmation',
             'forest_geometry_wgs84':geodetic(corrected_forest),
             'water_geometry_wgs84':terrain['water_geometry_wgs84'],
             'coverage_exempt_geometry_wgs84':geodetic(corrected_mask),
             'effective_area_geometry_wgs84':geodetic(corrected_effective),
             'forest_area_m2':corrected_forest.area,'water_area_m2':raw_water.area,
             'coverage_exempt_area_m2':corrected_mask.area,'effective_area_m2':corrected_effective.area,
             'removed_exemption_area_m2':stale_forest.area,
             'effective_area_gain_vs_primary_m2':corrected_effective.area-main_effective.area,
             'processing_note':'局部修改已处理的森林覆盖豁免掩膜，不对裁剪后的全场地类再次buffer。仅去除候选同片旧树林的豁免部分；其他森林/水体及其既有5m/10m规则原样保留。新树林仍由强禁扫完整约束。'},
        ],
        'checks':{'user_chain_verbatim':True,'original_numbering_verified':True,
            'projection_unchanged':True,'pond_geometry_unchanged':True,
            'unrelated_exemption_unchanged_in_sensitivity':True,'all_water_unchanged':True,
            'new_forest_replaces_old_not_union':True,'new_forest_contains_original_anchor':True},
        'limitations':['敏感性不代表获准修改主方案或真实地类。','分类来源2021、影像2017，均非现状测绘。',
            '提供完整几何及面积；固定航点覆盖敏感性须在最终规划生成后另行计算。']}

    merged=deepcopy(old);merged['revision_version']=2;merged['prepared_date']=revision['date']
    merged['historical_basemap']['file']='inputs/v1/05_历史底图.jpg'
    merged['historical_basemap']['file_path_base']='delivery_root'
    cp=merged['common_parameters']
    for key in ['upper_scan_radius_m','lower_scan_radius_m','scan_seconds','horizontal_speed_mps',
                'takeoff_vertical_speed_mps','mission_limit_s','preferred_reserve_s']:
        cp[key]=override[key]
    cp.update(return_climb_mps=revision['return_climb_rate']['proposed_nominal_mps'],
        return_climb_status='planner_provisional_unconfirmed',
        return_climb_sensitivity_mps=revision['return_climb_rate']['sensitivity_mps'],
        return_endpoint='above_own_home_at_own_upper_altitude_for_dual_layer_or_assigned_altitude_for_fixed_layer_no_landing',
        dual_layer_return_policy=override['dual_layer_return_policy'],
        fixed_layer_return_policy=override['fixed_layer_scene_return_policy'])
    for scene in merged['scenes']:
        if scene['execution_mode']=='fixed_layer_forward_reverse':
            for lid in ('upper','lower'):
                for u in scene[lid+'_uavs'].values():u['radius_m']=cp[lid+'_scan_radius_m']
            scene['return_policy']=cp['fixed_layer_return_policy']
        else:scene['return_policy']=cp['dual_layer_return_policy']
    merged['user_confirmations']=[
        '决赛1各机固定一层，正逆两趟，每次扫描50秒；上组UAV1/2/3，下组UAV4/5/6。',
        '决赛2和许昌2总共上层一趟下层一趟，不在各层反复两趟。',
        '本次修订确认三场景均上层50m、下层30m扫描半径，5m/s、50秒/点、1380秒硬预算。',
        '双层高度对应及2m/s下降速度仍是用户接受的暂定方案。',
        '双层结束先回升至本机上层，经连接路线接入上层返航目录后返回；固定层场景不额外变层。',
        '用户提供8个树林边界点；Q南/Q东为开发对话建议的正交推导补点，非用户实测坐标。',
        '池塘边界仍为2017影像草案，回升速度2m/s是规划者未确认暂估。']
    for e in merged['final_field']['strict_scan_exclusions']:
        replacement=forest_record if e['id']=='southeast_exclusion_anchor' else pond_record
        e.update(deepcopy(replacement))
        e['description']=replacement['interpretation']
    merged['final_field']['terrain']['v2_mask_audit']={
        'selected_variant':'retain_historical_mask','audit_file':'terrain_mask_audit.json',
        'primary_geometry_unchanged_from_v1':True,
        'local_forest_pixel_ownership_status':'unconfirmed',
        'stale_exemption_candidate_area_m2':stale_forest.area,
        'hypothetical_local_correction_effective_area_m2':corrected_effective.area}
    # Do not rewrite any source landcover arrays or processed exemption geometry.
    for key in ('forest_geometry_wgs84','water_geometry_wgs84','coverage_exempt_geometry_wgs84'):
        assert merged['final_field']['terrain'][key]==terrain[key]
    unresolved=[r for r in merged['unresolved_or_provisional'] if r['id']!='new_exclusion_boundaries']
    unresolved += [
        {'id':'pond_boundary','status':'requires_confirmation','detail':'2017影像池塘候选完整保留，用户本次未确认。'},
        {'id':'forest_connectors','status':'developer_proposed_not_user_measured','detail':'用户8点保留；Q南/Q东来自原8—9、9—10边正交推导。'},
        {'id':'return_climb_rate','status':'planner_provisional_unconfirmed','detail':'回升暂估2m/s，必须列1/2/3m/s敏感性；不能用起飞6m/s代替。'},
        {'id':'historical_terrain_local_ownership','status':'requires_confirmation','detail':'旧候选内/新树林外历史森林豁免归属未确认，主方案保留，局部修正仅为敏感性。'},
        {'id':'current_topic_compatibility','status':'offline_validation_required','detail':'实际发送数组直连与返航目录须独立验证；scene1物理lower也映射到单层path_stage_1。'}]
    merged['unresolved_or_provisional']=unresolved
    merged['revision_provenance']={
        'original_inputs_sha256':sha(OLD),'original_result_sha256':sha(old_plan),
        'revision_request_sha256':sha(REVISION),'sources':provenance,
        'merge_script':'merge_inputs.py','canonical_input_file':'inputs/02_规划输入_v2.json',
        'precedence':'v2 user-confirmed overrides and explicit provisional proposals supersede v1; unchanged field geometry/projection/home/landcover/partitions remain inherited',
        'main_exclusions_are_embedded_here':True,'derived_exclusions_config':'exclusions_draft.json',
        'old_repository_source_files_retained_in_source_files':True,
        'is_flight_command':False}
    merged['v2_requirements']={
        'validate_transmitted_scan_arrays_direct_links':True,
        'validate_interlayer_direct_link':True,
        'non_scanning_auxiliary_points_not_supported_in_scan_arrays':True,
        'generate_all_upper_waypoint_returns_for_each_dual_layer_recipient':True,
        'iteratively_prune_redundant_scanning_points':True,
        'maintain_ros_topics_null_in_original_main_schema':True,
        'topic_mapping_file':'topic_mapping.json',
        'source_interface_description':'inputs/revision/当前程序B双层接口说明.txt'}
    assert merged['final_field']['projection']==old['final_field']['projection']
    assert merged['xuchang_field']==old['xuchang_field']
    save(NEW,merged)
    exclusions={
        'artifact_type':'v2_user_forest_and_historical_pond_exclusion_draft',
        'status':'draft_requires_pond_and_connector_confirmation',
        'source_date':revision['date'],'source':'inputs/02_规划输入_v2.json',
        'source_sha256':sha(NEW),'input_sha256':sha(NEW),
        'original_input_sha256':sha(OLD),'revision_request_sha256':sha(REVISION),
        'source_provenance':provenance,
        'is_current_survey':False,'is_user_confirmed_boundary':False,
        'method':'User forest chain with independently recomputed orthogonal original-edge connectors; unchanged historical pond. Forest replaces the old polygon completely.',
        'exclusions':[forest_record,pond_record],
        'combined_metrics':{'candidate_union_area_m2':strict_union.area,
            'overlap_area_m2':new_forest.area+pond.area-strict_union.area,
            'additional_area_removed_after_terrain_exemption_m2':strict_union.difference(old_mask).area,
            'draft_effective_area_m2':main_effective.area},
        'limitations':['池塘仍待确认；树林补点为推导建议而非用户实测。','强禁扫区不自动禁飞。',
            '历史地类主方案保持不变，局部修正敏感性见terrain_mask_audit.json。']}
    save(ROOT/'exclusions_draft.json',exclusions)
    audit['merged_input_sha256']=sha(NEW)
    save(ROOT/'terrain_mask_audit.json',audit)
    notes=[
        'v2 输入与强禁扫边界合并说明',
        '唯一执行输入：inputs/02_规划输入_v2.json；由merge_inputs.py合并不可变v1输入和修订需求生成。',
        '优先级：本次修订明确确认/暂定事项覆盖v1；未修改的原投影、原编号外界、参考起点、许昌分区与通道继续继承。',
        '三个场景上层半径50m、下层30m；所有公共参数从合并输入读取。',
        '双层结束先在下层末点回升13m至本机上层，计入2m/s暂估回升时间和返航接入距离；2m/s非用户确认，另算1/2/3m/s敏感性。',
        '树林原8个用户点一字不动；按原编号8—9南界及9—10东界在原米制投影正交求Q南/Q东。补点是开发对话建议推导，非用户实测。',
        f"Q南[纬度,经度]={q_south_latlon}；Q东={q_east_latlon}。",
        f"P1至东界{math.dist(chain_nw[0],qe):.6f}m；P8至南界{math.dist(chain_nw[-1],qs):.6f}m。",
        '外环严格P1…P8→Q南→原9号→Q东→P1，既非P8直连P1、凸包，也非旧候选与新边界的并集。',
        f"旧树林候选{old_forest.area:.6f}㎡；新树林{new_forest.area:.6f}㎡；新范围在旧范围外{new_forest.difference(old_forest).area:.6f}㎡。",
        f"池塘{pond.area:.6f}㎡，原几何完整保留；日期2017-07-24，仅输入历史元数据，仍未确认。",
        f"原有效面积{old_effective.area:.6f}㎡；替换树林、保留历史mask后的v2主方案{main_effective.area:.6f}㎡。",
        f"旧候选内/新树林外仍有历史森林豁免{stale_forest.area:.6f}㎡；对应同片树林归属未确认。",
        f"仅作局部森林修正假设时有效面积{corrected_effective.area:.6f}㎡，增加{corrected_effective.area-main_effective.area:.6f}㎡。",
        '主方案不采用局部修正；完整保留/局部修正几何、面积及无关森林/水体保持检查见terrain_mask_audit.json。',
        '局部修正对已经处理的覆盖豁免掩膜做精确差集，避免对已裁切地类再次内缩导致全场其他边缘改变；新树林内部仍被强禁扫扣除。',
        '来源清单及SHA256同时写入新输入revision_provenance、exclusions_draft.json及terrain_mask_audit.json。旧source_files继续记录原项目来源，但复算不依赖原项目路径。',
        '运行：python3 merge_inputs.py；依赖：Python 3及Shapely。所有输入路径相对脚本目录。',
        '输入或边界确认变化后须重新优化全部三个场景并重新验证，不能仅更改显示或半径标签。']
    (ROOT/'exclusions_notes.txt').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    print(json.dumps({'input':str(NEW.relative_to(ROOT)),'sha256':sha(NEW),
        'new_forest_area_m2':new_forest.area,'pond_area_m2':pond.area,
        'primary_effective_area_m2':main_effective.area,'stale_forest_exemption_m2':stale_forest.area,
        'corrected_hypothesis_effective_area_m2':corrected_effective.area},ensure_ascii=False))

if __name__=='__main__':main()
