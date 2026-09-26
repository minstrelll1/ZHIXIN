"""导出六机紧凑分区、悬停扫描、地类边界和总机时报告。"""
import argparse
import json
import re
import sys
from html import escape
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'competition_backend'))
from competition_backend import polygon_coverage
parser=argparse.ArgumentParser(description='导出科目一二紧凑悬停侦察方案')
parser.add_argument('--rebuild',action='store_true',help='仅赛前使用：重新求解并保存固定方案')
parser.add_argument('--uav-count',type=int,default=6,help='预计算的无人机数量（1～6）')
parser.add_argument('--output-dir',type=Path,default=ROOT/'docs')
args=parser.parse_args()
plan=polygon_coverage.plan_competition_coverage(rebuild=args.rebuild,uav_count=args.uav_count)
if args.rebuild:
    polygon_coverage.save_prepared_plan(plan)
out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
a=plan['search_area'];c=a['coverage'];t=a['terrain'];proj=c['projection'];margin=35
width,height=a['width_m']+2*margin,a['height_m']+2*margin
colors=['#1261be','#dc6b12','#129061','#ce3949','#8254bd','#8d6529']
def xy(p):return (a['origin_y_m']+a['width_m']-p[1]+margin,a['origin_x_m']+a['height_m']-p[0]+margin)
def gps(p):return [proj['longitude']-p[1]/proj['west_m_per_degree'],proj['latitude']+p[0]/proj['north_m_per_degree']]
def local(lat,lon):return [(lat-proj['latitude'])*proj['north_m_per_degree'],-(lon-proj['longitude'])*proj['west_m_per_degree']]
def coordinates(points):return ' '.join('%.3f,%.3f'%xy(p) for p in points)
def geo_path(g):
    polygons=[g['coordinates']] if g['type']=='Polygon' else g['coordinates'] if g['type']=='MultiPolygon' else []
    return ' '.join('M'+' L'.join('%.3f,%.3f'%xy(p) for p in ring)+' Z' for poly in polygons for ring in poly)
def geo_gps(g):
    if g['type']=='Polygon':return {'type':'Polygon','coordinates':[[gps(p) for p in ring] for ring in g['coordinates']]}
    if g['type']=='MultiPolygon':return {'type':'MultiPolygon','coordinates':[[[gps(p) for p in ring] for ring in poly] for poly in g['coordinates']]}
    return {'type':'GeometryCollection','geometries':[]}
svg=[];rows=[];legend=[];features=[]
svg.append(f'<defs><clipPath id="clip"><polygon points="{coordinates(a["points_m"])}"/></clipPath></defs>')
# 复用既有报告中的已保存影像，不宣称为现状影像。
source=ROOT/'docs/subject12_area_report.html'
if source.exists():
    original=source.read_text(encoding='utf-8-sig')
    im=re.search(r'data:image/jpeg;base64,([A-Za-z0-9+/=]+)',original)
    data=re.search(r'<script[^>]*id="report-data"[^>]*>(.*?)</script>',original,re.S)
    if im and data:
        bounds=json.loads(data.group(1))['image_bounds'];south,west=bounds[0];north,east=bounds[1]
        x,y=xy(local(north,west));xx,yy=xy(local(south,east))
        svg.append(f'<image id="satellite" href="{im.group(0)}" x="{x}" y="{y}" width="{xx-x}" height="{yy-y}" opacity=".65" clip-path="url(#clip)" style="display:none"/>')
features.append({'type':'Feature','properties':{'名称':'比赛边界'},'geometry':{'type':'Polygon','coordinates':[[gps(p) for p in a['points_m']+[a['points_m'][0]]]]}})
for name,color,label in [('forest','#399352','树木覆盖'),('water','#3298db','水体分类'),('forest_edge','#f2b93b','保留林缘'),('excluded','#485461','免侦察内部')]:
    geo=a['terrain_layers_m'][name]
    svg.append(f'<path class="terrain" data-layer="{name}" d="{geo_path(geo)}" fill="{color}" fill-opacity=".38" fill-rule="evenodd" stroke="{color}" stroke-width=".4"><title>{label}</title></path>')
    features.append({'type':'Feature','properties':{'名称':label,'source_year':2021,'resolution_m':10},'geometry':geo_gps(geo)})
for (key,u),color in zip(plan['planned_uavs'].items(),colors):
    task=u['task'];route=task['flight_path_m'];scan=task['scan_waypoints_m'];poly=task['polygon_m'];m=task['shape']
    polygeo={'type':'Polygon','coordinates':[[gps(p) for p in poly+[poly[0]]]]}
    features.append({'type':'Feature','properties':{'uav_id':int(key),'区域面积_m2':task['area_m2'],'长宽比':m['aspect_ratio']},'geometry':polygeo})
    if len(route)>=2:features.append({'type':'Feature','properties':{'uav_id':int(key),'行程_m':task['route_distance_m'],'时间_s':task['mission_time_s']},'geometry':{'type':'LineString','coordinates':[gps(p) for p in route]}})
    svg.append(f'<g class="uav" data-uav="{key}"><polygon points="{coordinates(poly)}" fill="{color}" fill-opacity=".07" stroke="{color}" stroke-width="2"/>')
    svg.append('<g class="footprints" clip-path="url(#clip)" style="display:none">')
    for p in scan:
        x,y=xy(p);svg.append(f'<circle cx="{x}" cy="{y}" r="{c["reconnaissance_radius_m"]}" fill="{color}" fill-opacity=".09"/>')
    svg.append('</g>')
    svg.append(f'<polyline points="{coordinates(route)}" fill="none" stroke="{color}" stroke-width="2"/>')
    for i,p in enumerate(scan,1):
        x,y=xy(p);svg.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{color}"><title>UAV{key} 航点{i} · 悬停扫描 {c["hover_scan_seconds"]:g} 秒</title></circle><text class="numbers" x="{x+5}" y="{y-5}" font-size="10" fill="{color}" style="display:none">{i}</text>')
        features.append({'type':'Feature','properties':{'uav_id':int(key),'航点序号':i,'扫描_s':c['hover_scan_seconds']},'geometry':{'type':'Point','coordinates':gps(p)}})
    if scan:
        x,y=xy(scan[0]);svg.append(f'<text x="{x+8}" y="{y+18}" font-size="16" font-weight="bold" fill="{color}">UAV{key}</text>')
    svg.append('</g>')
    rows.append(f'<tr><td><i style="background:{color}"></i>UAV{key}</td><td>{task["area_m2"]/10000:.3f}</td><td>{task["required_area_m2"]/10000:.3f}</td><td>{m["long_side_m"]:.1f} × {m["short_side_m"]:.1f}</td><td>{m["aspect_ratio"]:.2f}:1</td><td>{task["route_distance_m"]:.1f}</td><td>{task["scan_count"]}</td><td>{task["travel_time_s"]:.1f}</td><td>{task["hover_scan_time_s"]:.1f}</td><td><b>{task["mission_time_s"]:.1f}</b></td></tr>')
    legend.append(f'<label><input type="checkbox" data-uav-toggle="{key}" checked><i style="background:{color}"></i>UAV{key}</label>')
svg.append(f'<polygon points="{coordinates(a["points_m"])}" fill="none" stroke="#172a3a" stroke-width="2.4" stroke-dasharray="7 4"/>')
html='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>固定方案 · 紧凑分区与悬停侦察</title><style>
*{box-sizing:border-box}body{margin:0;background:#edf2f6;color:#203449;font:15px/1.7 "Microsoft YaHei",sans-serif}main{max-width:1450px;margin:auto;padding:30px}h1{font-size:30px;margin:6px 0}h2{font-size:19px;margin:12px 0}p{margin:8px 0}.sub{color:#61758a}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:22px 0}.stat,.panel{background:#fff;border:1px solid #dbe4ec;border-radius:14px;padding:20px}.stat strong{display:block;font-size:27px;color:#155da4}.layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:20px}svg{display:block;width:100%;max-height:820px;touch-action:none;cursor:grab}label{display:block;margin:8px 0}i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:6px}button,a.download{border:1px solid #bdcfdf;border-radius:7px;background:#fff;color:#215b8a;padding:7px 10px;cursor:pointer;text-decoration:none;display:inline-block;margin:4px 4px 4px 0}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}td,th{white-space:nowrap;text-align:left;padding:12px;border-bottom:1px solid #e1e8f0}th{background:#f1f6fa}.notice{background:#fff6de;border-left:4px solid #d8a229;padding:12px;margin:14px 0}.formula{background:#eef5fc;padding:14px;font-size:18px}.panel+section{margin-top:20px}@media(max-width:850px){main{padding:14px}.stats{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}}</style></head><body><main>
<div class="sub">智信 · 科目一 / 科目二 · 规划预览</div><h1>紧凑分区与悬停侦察</h1><p class="sub">__COUNT__ 架无人机 · WGS84 实际边界 · 优化任务时间之和 · 林缘保留 5 米</p>
<div class="notice"><b>方案已固化 · Google Earth 最新地类待核验</b><br>本页直接显示本地保存的区域、航线和图层，离线可用；切换图层不下载底图、不重新求解。当前地类仍为 2021 年分类，旧底图为 2017 年影像，均不是最新 Google Earth 影像。</div><p class="sub">方案编号 __PLANID__ · 生成时间 __GENERATED__ · 无人机数量 __COUNT__</p><div class="stats"><div class="stat">总机时<strong>__SUM__ 秒</strong></div><div class="stat">并行完成<strong>__MAX__ 秒</strong></div><div class="stat">悬停扫描次数<strong>__SCANS__ 次</strong></div><div class="stat">应侦察区域覆盖<strong>100.00%</strong></div></div>
<div class="formula">优化目标：最小化 Σ（单机行程 ÷ __SPEED__ + 扫描次数 × __HOVER__）。总机时是各架无人机各自时间之和；并行完成时间另列。</div>
<div class="layout" style="margin-top:20px"><section class="panel"><button id="reset">重置视图</button><button id="zoomIn">放大</button><button id="zoomOut">缩小</button><span class="sub">滚轮缩放 / 拖动 / 北向上</span><svg id="map" viewBox="0 0 __W__ __H__" role="img" aria-label="紧凑六分区、悬停扫描点、林缘和免侦察区域">__SVG__</svg></section><aside class="panel"><h2>无人机与分区</h2>__LEGEND__<hr><h2>地图图层</h2><label><input id="satelliteToggle" type="checkbox">2017-07-24 卫星底图</label><label><input id="terrainToggle" type="checkbox" checked>地类与免侦察范围</label><label><input id="footprintsToggle" type="checkbox">悬停扫描覆盖圆</label><label><input id="numbersToggle" type="checkbox">航点序号</label><p><i style="background:#399352"></i>树木覆盖　<i style="background:#f2b93b"></i>保留林缘<br><i style="background:#485461"></i>免侦察内部　<i style="background:#3298db"></i>水体分类</p><h2>当前参数</h2><p>侦察半径 __RADIUS__ 米<br>速度 __SPEED__ 米/秒<br>每次扫描 __HOVER__ 秒<br>子区长宽比 ≤ __ASPECT__:1<br>林缘向内保留 __EDGE__ 米<br>不限制最小转弯半径</p><h2>下载</h2><a class="download" href="subject12_coverage_plan.json" download>完整方案</a><a class="download" href="subject12_coverage_plan.geojson" download>路线与地类 GeoJSON</a><a class="download" href="subject12_landcover.geojson" download>原始地类边界</a></aside></div>
<section class="panel" style="margin-top:20px"><h2>任务分派与时间</h2><div class="scroll"><table><thead><tr><th>无人机</th><th>区域 / 公顷</th><th>应侦察 / 公顷</th><th>长边 × 短边 / 米</th><th>长宽比</th><th>行程 / 米</th><th>扫描次数</th><th>飞行 / 秒</th><th>扫描 / 秒</th><th>总时长 / 秒</th></tr></thead><tbody>__ROWS__</tbody></table></div><p>总面积 __AREA__ 公顷，应侦察 __REQUIRED__ 公顷，地图推定免侦察 __EXCLUDED__ 公顷。总行程 __DISTANCE__ 米。</p><p>长宽比取各子区最小面积旋转外接矩形的长边 / 短边。本次最大 __ACTUAL_ASPECT__:1；每区保持连通，区域之间不重叠。</p></section>
<section class="panel" style="margin-top:20px"><h2>森林、林缘与水体</h2><p>2026-09-16 已查看 <a href="https://earth.google.com/web/@33.865,113.709,0a,2500d,35y,0h,0t,0r" target="_blank" rel="noopener">Google Earth 场地视图</a>，其时间轴最新可选日期显示为 <b>2021-11-08</b>。影像中的林地与池塘可辨认，但当前未完成具有地理定位信息的本地影像导出及边界提取，所以本方案的免侦察地类仍沿用下述 ESA 分类。这个链接仅供赛前核验，本页不会自动打开或刷新 Google Earth。</p><p>使用 <a href="https://esa-worldcover.org/en/data-access">ESA WorldCover 2021 v200</a> 的 10 米分类栅格，直接读取类别值并转为 WGS84 多边形，未按底图颜色自动猜测。树木覆盖区作为林地代理，可能含果园；只有距林地边缘超过 __EDGE__ 米的内部可以免侦察。窄林带会全部保留。水体向内退让 10 米后才扣除，岸线继续扫描。</p><p>比赛区域内分类树木覆盖 __FOREST__ 公顷，其中保留林缘 __FORESTEDGE__ 公顷；分类水体仅 __WATER__ 平方米，没有产生可扣除的水体内部。</p><div class="notice">__CONFIDENCE__ 旧卫星影像显示部分疑似池塘，当前未取得可靠现状岸线，因此这些疑似池塘继续侦察。启用的免侦察范围是历史地图推定，用于方案比较；不是现状测绘结论。</div><p class="sub">__ATTRIBUTION__<br>可选卫星底图：Esri / Vantor / Earthstar Geographics / GIS User Community，拍摄日期 2017-07-24，复用既有场地报告。数据查询：2026-09-16。</p></section>
<section class="panel" style="margin-top:20px"><h2>优化与执行范围</h2><p>赛前在多个方向中搜索 __COUNT__ 个连通紧凑子区，约束长宽比上限和区域面积范围；保留 __CANDIDATES__ 组候选并计算离散圆覆盖。悬停点按新增覆盖面积选择、补齐遗漏并删去冗余点；区域内连接采用可视路径，再用多起点与 2-opt 改善访问顺序。最终先比较总机时，再比较并行完成时间。本方案没有全局最优证明。</p><p>100% 覆盖指所有应侦察地面的水平几何覆盖，不把飞行途中路段当作扫描。航点之间可能存在用于避开凹口的航段折点，这些折点不计为扫描点。地类免侦察不等于禁飞区；未加入地形遮挡、建筑高度、起降/返航、加减速和多机时空冲突。</p><p>紧凑分区可以减少狭长形状及跨区边界暴露，但不能单独保证移动目标不被重复跟踪。目标 ID 合并、唯一跟踪责任机和跨区接管仍需后续协议；本次没有改动跟踪控制链路，也没有下发飞行任务。</p></section></main>
<script>const map=document.getElementById('map'),initial=[0,0,__W__,__H__];const stateKey='zhixin.coverage.view.'+'__PLANID__';let stored={};try{stored=JSON.parse(localStorage.getItem(stateKey)||'{}')||{}}catch{}let view=Array.isArray(stored.view)&&stored.view.length===4&&stored.view.every(Number.isFinite)&&stored.view[2]>0&&stored.view[3]>0?stored.view:[...initial],drag=null;function save(){try{localStorage.setItem(stateKey,JSON.stringify({view,checks:Object.fromEntries([...document.querySelectorAll('input[type=checkbox]')].map(el=>[el.id||'uav'+el.dataset.uavToggle,el.checked]))}))}catch{}}function draw(){map.setAttribute('viewBox',view.join(' '));save()}function zoom(k){view=[view[0]+view[2]*(1-k)/2,view[1]+view[3]*(1-k)/2,view[2]*k,view[3]*k];draw()}document.getElementById('reset').onclick=()=>{view=[...initial];draw()};document.getElementById('zoomIn').onclick=()=>zoom(.8);document.getElementById('zoomOut').onclick=()=>zoom(1.25);map.onwheel=e=>{e.preventDefault();zoom(e.deltaY>0?1.1:.9)};map.onpointerdown=e=>{drag=[e.clientX,e.clientY,...view];map.setPointerCapture(e.pointerId)};map.onpointermove=e=>{if(!drag)return;const r=map.getBoundingClientRect(),scale=Math.min(r.width/drag[4],r.height/drag[5]);view[0]=drag[2]-(e.clientX-drag[0])/scale;view[1]=drag[3]-(e.clientY-drag[1])/scale;draw()};map.onpointerup=map.onpointercancel=()=>drag=null;document.querySelectorAll('[data-uav-toggle]').forEach(el=>el.onchange=()=>document.querySelector(`[data-uav="${el.dataset.uavToggle}"]`).style.display=el.checked?'':'none');for(const [id,selector] of [['satelliteToggle','#satellite'],['terrainToggle','.terrain'],['footprintsToggle','.footprints'],['numbersToggle','.numbers']])document.getElementById(id).onchange=e=>document.querySelectorAll(selector).forEach(el=>el.style.display=e.target.checked?'':'none');for(const el of document.querySelectorAll('input[type=checkbox]')){const key=el.id||'uav'+el.dataset.uavToggle;if(typeof stored.checks?.[key]==='boolean'){el.checked=stored.checks[key];el.dispatchEvent(new Event('change'))}el.addEventListener('change',save)}draw();</script></body></html>'''
values={'SUM':f'{c["total_mission_time_s"]:.1f}','MAX':f'{c["maximum_completion_time_s"]:.1f}','SCANS':c['total_scan_count'],
'COUNT':len(plan['planned_uavs']),'PLANID':plan['prepared_plan']['id'],'GENERATED':escape(plan['prepared_plan']['generated_at']),'W':width,'H':height,'SVG':''.join(svg),'ROWS':''.join(rows),'LEGEND':''.join(legend),'SPEED':c['speed_mps'],'HOVER':c['hover_scan_seconds'],
'RADIUS':c['reconnaissance_radius_m'],'ASPECT':c['max_region_aspect_ratio'],'EDGE':t['forest_edge_m'],'AREA':f'{c["area_m2"]/10000:.3f}',
'REQUIRED':f'{c["required_area_m2"]/10000:.3f}','EXCLUDED':f'{c["excluded_area_m2"]/10000:.3f}','DISTANCE':f'{c["total_distance_m"]:.1f}',
'ACTUAL_ASPECT':f'{c["actual_max_aspect_ratio"]:.2f}','FOREST':f'{t["forest_area_m2"]/10000:.3f}','FORESTEDGE':f'{t["forest_edge_area_m2"]/10000:.3f}',
'WATER':f'{t["water_area_m2"]:.1f}','CONFIDENCE':escape(t['confidence_note']),'ATTRIBUTION':escape(t['attribution']),'CANDIDATES':c['evaluated_partitions']}
for key,value in values.items():html=html.replace('__'+key+'__',str(value))
(out/'subject12_coverage_plan.html').write_text(html,encoding='utf-8')
(out/'subject12_coverage_plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
(out/'subject12_coverage_plan.geojson').write_text(json.dumps({'type':'FeatureCollection','features':features},ensure_ascii=False,indent=2),encoding='utf-8')
print('规划报告已生成：',out)
