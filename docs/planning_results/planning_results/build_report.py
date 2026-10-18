#!/usr/bin/env python3
"""Derive human-readable reports and sensitivity from the final serialized plan."""
import json, math, csv
from pathlib import Path
from shapely.geometry import Polygon, Point, shape
from shapely.ops import unary_union, transform
ROOT=Path(__file__).resolve().parent

def local(g,p):
    if g is None:return Polygon()
    return transform(lambda lon,lat,z=None:((lat-p['latitude'])*p['north_m_per_degree'],-(lon-p['longitude'])*p['west_m_per_degree']),shape(g))

def main():
    result=json.loads((ROOT/'planning_result.json').read_text(encoding='utf-8'));inp=json.loads((ROOT/'inputs/02_规划输入.json').read_text(encoding='utf-8'));comparison=json.loads((ROOT/'candidate_comparison.json').read_text(encoding='utf-8'));exc=json.loads((ROOT/'exclusions_draft.json').read_text(encoding='utf-8'))
    lines=[]
    def add(s=''):lines.append(s)
    add('六无人机三场景离线规划交接报告 / '+result['plan_id'])
    add('主方案指标来自最终 planning_result.json；候选比较、先验界与边界敏感性按所列文件独立推导。固定候选边界见 exclusions_draft.json，影像审查见 exclusions_review.png。')
    add('决赛1/2为候选禁扫边界草案 draft；许昌 validated 仅指固定输入与暂定名义参数下离线验证，不代表实飞许可或已适配原程序。')
    add('输出坐标：WGS84；局部[北,西,上]米；waypoints_wgs84=[纬度,经度]；GeoJSON及B提案=[经度,纬度,相对高度]。GeoJSON Z非椭球高。')
    add('返回终点为各机名义降落点上方，不计最后落地下降。没有虚构真实任务ID、校验、GPS或飞行状态。')
    add()
    add('硬约束与输入缺口')
    add('决赛仅使用原25点的前10点原序围合，非凸包；森林内缩5m、水体内缩10m已由输入处理，绝不再次内缩。')
    add('两个新强禁扫真实边界未知，2017-07-24影像候选是显式人工解释假设。东南候选选完整道路围合地块，未把“附近”改成固定半径圆。池塘沿可见岸线。两者强禁扫要求完整圆盘不侵入，不等于禁飞。')
    add('ESA WorldCover2021为约10m分类，非现状测绘；免扫区允许飞越和圆盘触及，但不设置扫描中心。强禁扫优先于地类豁免。')
    add('双层高度UAV1/4=47→34m，2/5=50→37m，3/6=53→40m；下降2m/s，均是接受的暂定参数、待实机核对。A机号上组1/2/3、下组4/5/6是本次规划约定。')
    add('固定参考起点不依赖任务发布者。各机真实本次开机起降GPS未知，未来必须独立接入并重算。高度相对各机起飞点。')
    add('决赛取消旧专用进返场通道、投影互不交叠、其他落点2.5m避让与固定门点；保守保留全外界中心线5m要求。不继承旧编号禁扫、building首点、UAV3/5特殊区及北补扫。')
    add('许昌保持输入当前六区与机号；分别保持old_entry_path_m及old_return_path_from_last_waypoint_m的中间门点/通道/辅助点，连接新扫描首末点并重新绕障；不再执行交换历史。六点禁飞区所有中心与线段净距≥5m；其内不侦察，扫描圆触及不属于强禁扫。')
    add('所有扫描主航段在本机子区内路由，共边不新增禁飞带；扫描圆可跨子区边界。每组分区覆盖完整任务范围，免扫部分并未从分区删掉。')
    add('A两层各自3区允许水平重叠；B六区、每机上下层共用同一区。C保留大小不均固定区。全部环/洞/多面保留；若polygon_m为null，后续单环接口必须扩展。')
    add()
    add('先验面积上界与时间下界（先于求解计算并保存 feasibility_bounds.json）')
    bounds=json.loads((ROOT/'feasibility_bounds.json').read_text(encoding='utf-8'))
    for row in bounds['scenes']:
        add('%s：有效面积 %.3f㎡；不计飞行的扫描圆面积及点数仅是乐观界。'%(row['scene_id'],row['effective_area_m2']))
        for q in row['bounds']:
            if 'minimum_independent_points_for_full_area' in q:add('  UAV%d/%s：理想100%%至少%d独立点、每点2遍；仅扫描加起飞下界%.3fs；未被该必要时间下界排除=%s。'%(q['uav_id'],q['layer_id'],q['minimum_independent_points_for_full_area'],q['full_coverage_time_lower_bound_s'],q['not_ruled_out_by_time_lower_bound']))
            elif 'uav_id' in q:add('  UAV%d：理想100%%至少 upper=%d / lower=%d 个独立点；仅扫描加垂直时间下界%.3fs；未被该必要时间下界排除=%s。'%(q['uav_id'],q['minimum_upper_points_for_full_area'],q['minimum_lower_points_for_full_area'],q['full_coverage_time_lower_bound_s'],q['not_ruled_out_by_time_lower_bound']))
            else:add('  '+q['description'])
    x5=next(q for row in bounds['scenes'] if row['scene_id']=='xuchang_small_2' for q in row['bounds'] if q.get('uav_id')==5)
    add('UAV5的许昌%.3f㎡固定区，仅上层就至少%d点×50=%ds；上下层至少%d点，含垂直时间下界%.3fs，因此固定分区双层100%%不可能。不得用重分区替换主方案。'%(x5['effective_area_m2'],x5['minimum_upper_points_for_full_area'],50*x5['minimum_upper_points_for_full_area'],x5['minimum_upper_points_for_full_area']+x5['minimum_lower_points_for_full_area'],x5['full_coverage_time_lower_bound_s']))
    add('以上是必要条件而非覆盖可达证明；实际圆外溢、同层重叠、强禁扫禁设圆心范围与绕障会进一步降低覆盖。未声称任何候选全局最优。')
    add()
    add('可复现优化')
    add('固定种子20261017作为版本约定；本算法无随机抽样，候选网格及并列排序完全确定。10m圆心格点，加每15m合法边缘候选；4m目标采样用于启发式增益，最后用完整多边形布尔运算重算，不把采样率当最终指标。')
    add('圆盘近似每四分圆128段、整圆512边。几何导出前1e-7m有效精度网格去除布尔运算退化小环；保持所有面积有效组件/洞，独立验证最终WGS84文件。')
    add('生成外界净距5.03m；禁飞圆角缓冲5.06m/象限8段，最小弦距5.06*cos(pi/32)>5.03m，防止绕障折线切入5m边界。强禁扫圆心缓冲r+0.05m，验证使用真实点到区距离而非仅近似圆。')
    add('初点按单点采样收益选取；每轮每层最多12个收益最高候选，逐一尝试全部路径插入位置。收益/新增秒数^0.6；双层另乘1/(0.25+本层已覆盖比例)，避免50m下层挤占30m上层。单层不加该权重。每次插入2-opt最多8轮，严格变短才接受。')
    add('可见性图最短合法路由，障碍与子区边界辅助顶点全部显式输出。双层末首点另考察8对近邻接续对，最小化路长+3*max(层间水平路长-20,0)，不得突破预算；先原位下降，再水平转移。')
    add('终止：候选列表中没有新增至少max(2样本,0.1%面积)且满足预算的入选点，或40轮上限。只搜索短名单，未证明其他候选无收益。余量方案也可能已有部分小区饱和而点数不变。')
    add('决赛比较有效面积均衡与总面积均衡两组确定性递归分割；A北1区/南2区，B北中南3带各2区；每候选同时求解点数、点位和顺序，以两层全场覆盖率之和优先、最慢机时间次优选分区。许昌不改分区。')
    add('两个完成候选均保存完整JSON：candidates/coverage_priority.json（每机≤1350s，推荐）；candidates/more_reserve.json（每机≤1230s）。候选明细及候选/样本数量见candidate_comparison.json。')
    add('精确复算：python3 planner.py；独立核验：python3 validate_plan.py；报告：python3 build_report.py；预览：python3 build_preview.py。路径全部相对脚本目录。')
    add()
    add('候选比较（每场景分别选择该候选预算下最优的两种分区之一）')
    add('候选 / 场景 / 分区 / 上层% / 下层% / 最慢秒 / 六机总秒')
    for c in comparison:
        add('%s / %s / %s%s / %.3f / %.3f / %.3f / %.3f'%(c['candidate'],c['scene_id'],c['variant'],' [选用]' if c['selected_partition'] else '',100*c['upper_coverage'],100*c['lower_coverage'],c['maximum_completion_time_s'],c['total_aircraft_time_s']))
    add('推荐coverage_priority，因为在1380秒硬上限内保留至少30秒余量，并提高可覆盖面积；若任务需更多等待，选more_reserve完整数据后重新验证与生成预览。')
    add()
    summary=[]
    for s in result['scenes']:
        m=s['scene_metrics'];add(s['display_name']+' / '+s['scene_id']+' / '+s['status'])
        add('全场有效面积%.3f㎡；上层%.4f%%；下层%.4f%%；两层都覆盖%.4f%%；至少一层覆盖%.4f%%。'%(m['upper']['effective_area_m2'],100*m['upper']['coverage_ratio'],100*m['lower']['coverage_ratio'],100*m['both_layers_coverage_ratio'],100*m['either_layer_coverage_ratio']))
        for lid in ['upper','lower']:
            c=m[lid];add('  %s：独立点%d；覆盖%.3f㎡；缺口%.3f㎡；同层冗余%.3f㎡，冗余率%.4f%%。'%(lid,c['independent_scan_count'],c['covered_area_m2'],c['uncovered_area_m2'],c['redundant_area_m2'],100*c['redundancy_ratio']))
        add('最慢机%.3fs（%.3f分钟）；六机总机时%.3fs；所有机≤1380s=%s。'%(m['maximum_completion_time_s'],m['maximum_completion_time_s']/60,m['total_aircraft_time_s'],m['all_aircraft_within_limit']))
        add('UAV / 上下层独立点 / 起飞 / 进场 / 扫描移动 / 扫描 / 层间 / 返场 / 总秒 / 剩余秒 / 加30秒后')
        for u in s['uavs']:
            t=u['timing'];counts='/'.join(l['layer_id']+':'+str(len(l['waypoint_ids'])) for l in u['layers'])
            add('%d / %s / %.3f / %.3f / %.3f / %.3f / %.3f / %.3f / %.3f / %.3f / %.3f'%(u['uav_id'],counts,t['takeoff_s'],t['entry_s'],t['scan_transit_s'],t['scan_s'],t['layer_transition_s'],t['return_s'],t['mission_time_s'],t['remaining_s'],t['budget_with_reserve_s']))
            for l in u['layers']:
                c=l['coverage'];add('  %s %.0fm/R%.0fm：有效%.3f㎡，覆盖%.3f㎡(%.4f%%)，缺口%.3f㎡，冗余%.3f㎡(%.3f%%)；每点%d遍。'%(l['layer_id'],l['altitude_m'],l['reconnaissance_radius_m'],c['effective_area_m2'],c['covered_area_m2'],100*c['coverage_ratio'],c['uncovered_area_m2'],c['redundant_area_m2'],100*c['redundancy_ratio'],l['scan_passes']))
                summary.append(dict(scene_id=s['scene_id'],uav_id=u['uav_id'],layer_id=l['layer_id'],point_count=len(l['waypoint_ids']),**{k:c[k] for k in ['effective_area_m2','covered_area_m2','uncovered_area_m2','coverage_ratio','redundant_area_m2','redundancy_ratio']},mission_time_s=t['mission_time_s'],remaining_s=t['remaining_s']))
            if u['layer_transition']:
                q=u['layer_transition'];add('  层间水平%.3fm（端点直线%.3fm），下降%.3fm，用时%.3fs；%s'%(q['horizontal_distance_m'],math.dist(u['layers'][0]['waypoints_m'][-1],u['layers'][1]['waypoints_m'][0]),q['vertical_distance_m'],q['duration_s'],q['note']))
        for c in m['partition_checks']:add('分区组%s：正面积重叠%.9f㎡；缝隙%.9f㎡；越界%.9f㎡；有效=%s。'%(c['group'],c['overlap_area_m2'],c['gap_area_m2'],c['outside_area_m2'],c['all_regions_valid']))
        add()
    sensitivity=[]
    add('强禁扫边界敏感性（固定当前航点，不重新优化；±10m不是测量置信区间）')
    for s in result['scenes'][:2]:
        p=s['projection'];boundary=local(s['boundary_geometry_wgs84'],p);strict=unary_union([local(e['geometry_wgs84'],p) for e in s['exclusions'] if e['kind']=='scan_disk_forbidden']);terrain=unary_union([local(e['geometry_wgs84'],p) for e in s['exclusions'] if e['kind']=='coverage_exempt'])
        for off in [-10,0,10]:
            draft=strict.buffer(off,quad_segs=128).intersection(boundary) if off else strict
            effective=boundary.difference(terrain.union(draft));entry=dict(scene_id=s['scene_id'],boundary_offset_m=off,effective_area_m2=effective.area,layers={})
            for lid,r in [('upper',30),('lower',50)]:
                pts=[q for u in s['uavs'] for l in u['layers'] if l['layer_id']==lid for q in l['waypoints_m']]
                violations=sum(Point(q).distance(draft)<r for q in pts)
                cover=unary_union([Point(q).buffer(r,quad_segs=128) for q in pts]).intersection(effective)
                entry['layers'][lid]=dict(forbidden_disk_count=violations,fixed_point_coverage_ratio=cover.area/effective.area)
            sensitivity.append(entry);add('%s 边界偏移%+dm：有效%.3f㎡；上层候选侵入%d圆/几何覆盖%.3f%%；下层候选侵入%d圆/几何覆盖%.3f%%。'%(s['scene_id'],off,effective.area,entry['layers']['upper']['forbidden_disk_count'],100*entry['layers']['upper']['fixed_point_coverage_ratio'],entry['layers']['lower']['forbidden_disk_count'],100*entry['layers']['lower']['fixed_point_coverage_ratio']))
    add('外扩后如存在侵入，旧航点失效，必须按确认边界重新运行planner与validator；上面的覆盖比例不是合法性声明。候选面积和圆心禁设面积敏感性另见exclusions_draft.json。')
    add()
    add('覆盖口径与精度')
    add('本机层分母=本机子区∩有效目标区；全场层分母=有效目标区。有效目标区扣免扫与强禁扫（许昌扣禁飞）。分子为独立扫描圆并集∩分母。A重复扫描不增加几何覆盖。上下层不得相加冒充一层100%。')
    add('同层冗余=各独立圆裁剪面积和−并集面积，冗余率=冗余/该面积和；跨层重复是需求。完整未覆盖几何在JSON/GeoJSON，圆外溢不计覆盖，圆跨子区在全场层统计中正确归并。')
    add('验证器从最终文件独立复算：1e-4m坐标、0.01㎡面积、0.1s时间总账容差，仅用于数值比较，5m和1380s硬限制不实质放宽。schema通过不是几何/时间通过。')
    if (ROOT/'validation_report.json').exists():
        vr=json.loads((ROOT/'validation_report.json').read_text(encoding='utf-8'));add('当前验证摘要见validation_report.json（由独立脚本生成），报告状态：'+str(vr.get('status',vr.get('overall_status',vr.get('passed','见文件')))))
    add()
    add('后续程序适配与未验证项')
    add('交还planning_result.json、validate_plan.py、validation_report.json、planner.py、exclusions_draft.json、planning_report.txt、planning_preview.html及输入schema。建议整个zip交还，避免配置/依赖缺失。')
    add('需新增3个scene_id与层数据接口；旧单层task/sector/scans需扩展upper/lower与各层独立高度、半径、点位、顺序；严格保留A scan_passes=2而B/C每层=1。')
    add('A执行P1…PN,PN…P1，转折PN两条连续50s记录；B/C上层一次，原位下降13m，再水平至下层首点一次，最终由下层末点返场。辅助点只飞不扫描。')
    add('B的upper/lower是proposed_only逻辑载荷、ros_topics=null，不是已存在话题或协议。若B自己循环，A仅发送一趟唯一点与遍数，不能把展开序列再循环两遍。')
    add('真实GPS、动态防撞、航线同时相交、通信失联策略、飞控加减速、耗电/风场、目标跟踪、地理围栏授权未验证；没有做真实飞行指令或外部接口调用。按输入建议保留的30s规划余量用于未知耗时，不是实飞保障。')
    add('独立3D预览双击打开即可，内嵌最终JSON，不需要服务器、网络或原项目；三场景/六机/上下层筛选，显示扫描圆、分区、禁区、航线、时间及覆盖；默认真实XYZ比例，任何Z夸张明确标识。')
    add('输入/输出哈希见SHA256SUMS.json及SHA256SUMS.txt；文件自身哈希清单与ZIP的哈希按README说明避免自引用。')
    (ROOT/'planning_report.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (ROOT/'boundary_sensitivity.json').write_text(json.dumps(sensitivity,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    with (ROOT/'mission_summary.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0]));w.writeheader();w.writerows(summary)
    print('Wrote planning_report.txt, mission_summary.csv, boundary_sensitivity.json')
if __name__=='__main__':main()
