"""从当前固定方案生成可离线打开的六机三维航线报告。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "competition_backend"))
sys.path.insert(0, str(ROOT / "competition_shared"))
from competition_backend.transit_routes import CACHE, PROFILES, scene_plan  # noqa: E402


def build_data():
    cache = json.loads(CACHE.read_text(encoding="utf-8"))["scenes"]
    config = json.loads((ROOT / "competition_backend/config/competition.example.json").read_text(encoding="utf-8"))
    heights = config["subjects"]["subject1"]["takeoff_altitudes_m_by_profile"]
    scenes = {}
    for profile in PROFILES:
        departures = (("fixed_dalian",) if profile == "dalian_nanshan" else
                      ("fixed_xuchang",) if profile == "xuchang_small" else
                      ("southeast", "stadium_center"))
        for departure in departures:
            key = profile + "/" + departure
            plan = scene_plan(profile, departure)
            saved = cache[key]
            uavs = {}
            fleet_returns = [(str(uid) + ':' + str(i + 1), path)
                             for uid, vehicle in sorted(saved['vehicles'].items(), key=lambda pair: int(pair[0]))
                             for i, path in enumerate(vehicle['return_paths_m'])]
            for uid, item in plan["planned_uavs"].items():
                route = saved["vehicles"][str(uid)]
                uavs[str(uid)] = {
                    "sector": item["task"]["polygon_m"],
                    "scans": item["task"]["waypoints_m"],
                    "entry": route["entry_path_m"],
                    "returns": [path for _, path in fleet_returns],
                    "return_keys": [key for key, _ in fleet_returns],
                }
            scenes[key] = {
                "boundary": saved["boundary_m"],
                "exclusions": saved.get("excluded_polygons_m", []),
                "safe_exclusions": saved.get("safe_excluded_polygons_m", []),
                "safe_boundary": saved.get("safe_boundary_m", []),
                "boundary_clearance_m": saved.get("boundary_clearance_m", 0),
                "endpoint_clearance_policy": saved.get("endpoint_clearance_policy", ""),
                "departure": saved["departure_m"],
                "uavs": uavs,
            }
    return {"scenes": scenes, "heights": heights}


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>六机任务三维规划总览</title>
<style>
:root{color-scheme:dark;font-family:"Microsoft YaHei",system-ui,sans-serif}*{box-sizing:border-box}
body{margin:0;background:#0b1422;color:#e7eef8}header{padding:18px 24px;background:#102238;border-bottom:1px solid #294461}
h1{margin:0;font-size:23px}header p{margin:7px 0 0;color:#b8cbe0;font-size:14px}
.bar{display:flex;flex-wrap:wrap;gap:10px;padding:13px 20px;background:#12253b;align-items:end}
label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:#b9cee4}select,input[type=file]{border:1px solid #456384;border-radius:7px;background:#091a2c;color:#f0f7ff;padding:7px;min-width:120px}
input[type=range]{width:120px}button{background:#1d5a83;color:#fff;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px;padding:14px;min-height:calc(100vh - 158px)}
canvas{display:block;width:100%;height:100%;min-height:550px;background:linear-gradient(#11243a,#0b1828);border:1px solid #395573;border-radius:10px;touch-action:none}
aside{background:#102238;border:1px solid #36516c;border-radius:10px;padding:16px;font-size:14px;line-height:1.65}
aside h2{margin:0 0 8px;font-size:18px}.hint{color:#b6cbe1}.warning{background:#503a19;color:#ffe7ab;border-radius:6px;padding:9px;margin-top:14px}
.legend{display:flex;flex-wrap:wrap;gap:8px;font-size:12px}.legend span{padding:3px 7px;border-radius:4px;background:#203954}
@media(max-width:900px){.layout{grid-template-columns:1fr}canvas{height:65vh;min-height:400px}}
</style></head><body>
<header><h1>六机任务三维规划总览</h1><p>固定侦察分区与航点 · 进场航线 · 逐航点返航 · 各机相对起飞高度。鼠标拖动旋转，滚轮缩放。</p></header>
<div class="bar">
<label>飞行场景<select id="scene"></select></label><label>出发点<select id="departure"></select></label>
<label>高度方案<select id="height"></select></label><label>返航接收机<select id="uav"></select></label>
<label>返航起点<select id="returnIndex"></select></label>
<label>高度显示倍率 <span id="zValue"></span><input id="zScale" type="range" min="1" max="100" value="35"></label>
<label>导入本次规划 JSON（可选）<input id="import" type="file" accept=".json,application/json"></label>
<button id="reset">恢复视角</button>
</div>
<div class="layout"><canvas id="view"></canvas><aside><h2 id="summary">规划概览</h2><div id="details"></div>
<p class="hint">蓝色为飞向任务区，彩色为侦察航点与分区，橙色为返航线，红色为降落点；竖线表示相对起飞高度。</p>
<p class="hint" id="clearanceNote" hidden></p>
<div class="legend" id="legend"></div>
<p class="warning" id="homeNote">离线固定方案的降落标记只是示意。GPS 实飞规划先取得每架已连接无人机的有效经纬度，再以各机自身起飞点重接进场和返航路径。导入本次规划 JSON 可显示实飞规划点。</p>
<p class="hint" id="cursor">将鼠标移至航点查看编号。</p></aside></div>
<script id="planning-data" type="application/json">__DATA__</script>
<script>
const DATA=JSON.parse(document.getElementById('planning-data').textContent);
const names={lab:'3m×3m',outdoor5:'5m×5m',lab10:'10m×10m',outdoor100:'100m×100m',outdoor200:'200m×200m',competition:'竞赛 1km×1km',dalian_nanshan:'大连南山坡外场',xuchang_small:'许昌试飞场地（小）'};
const colors=['#27a0ff','#42d672','#ff923d','#c88cff','#41d5df','#ff5275'];
const byId=id=>document.getElementById(id), canvas=byId('view'),ctx=canvas.getContext('2d');
let angle=-0.55,pitch=0.80,zoom=1,drag=false,last=null,live=null,hovered='';
function fillSelect(id,items,current){const s=byId(id);s.innerHTML='';for(const [value,label] of items){const o=document.createElement('option');o.value=value;o.textContent=label;s.append(o)}if(current&&items.some(x=>x[0]===current))s.value=current}
fillSelect('scene',Object.keys(names).map(k=>[k,names[k]]),'competition');
fillSelect('height',[['default','场景默认'],['around1m','约 1m'],['around2m','约 2m'],['around5m','约 5m'],['around10m','约 10m'],['around45m','约 45m'],['around54m','约 54m：64/60/56/52/48/44m']],'default');
fillSelect('uav',[['all','全部'],...Array.from({length:6},(_,i)=>[String(i+1),'UAV'+(i+1)])],'all');
function departures(){const scene=byId('scene').value;fillSelect('departure',scene==='dalian_nanshan'?[['fixed_dalian','大连固定起飞点']]:scene==='xuchang_small'?[['fixed_xuchang','许昌固定起飞点']]:[['southeast','区域右下角'],['stadium_center','操场中央']]);render()}
function profileKey(){let choice=byId('height').value;if(choice==='around1m')return 'lab';if(choice==='around2m')return 'lab2';if(choice==='around5m')return 'lab5';if(choice==='around10m')return 'around10m';if(choice==='around45m')return 'competition';if(choice==='around54m')return 'around54m';let scene=byId('scene').value;return ['competition','outdoor100','outdoor200','dalian_nanshan','xuchang_small'].includes(scene)?'competition':scene==='outdoor5'?'lab5':'lab'}
function current(){const key=byId('scene').value+'/'+byId('departure').value;return live&&live.key===key?live.data:DATA.scenes[key]}
function bounds(data){const points=[...data.boundary,data.departure,...Object.values(data.uavs).flatMap(u=>u.scans)];const xs=points.map(p=>-p[1]),ys=points.map(p=>p[0]);return {cx:(Math.min(...xs)+Math.max(...xs))/2,cy:(Math.min(...ys)+Math.max(...ys))/2,span:Math.max(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys),1)}}
function stage(data){const b=bounds(data),w=canvas.width,h=canvas.height,base=Math.min(w,h)*.72/b.span;
let hs=data.heights||DATA.heights[profileKey()],high=Math.max(...Object.values(hs).map(Number));let exaggeration=Number(byId('zScale').value)*b.span/(Math.max(high,1)*100);
byId('zValue').textContent='×'+exaggeration.toFixed(1);
function point(p,z=0){const east=-p[1]-b.cx,north=p[0]-b.cy;const x=east*Math.cos(angle)-north*Math.sin(angle),y=east*Math.sin(angle)+north*Math.cos(angle);return [w/2+x*base*zoom,h/2-(y*Math.sin(pitch)+z*exaggeration*Math.cos(pitch))*base*zoom]}
return {point,b,hs,high}}
function path(points,z,color,width=2,dash=[],alpha=1){if(!points||points.length<2)return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=width*devicePixelRatio;ctx.globalAlpha=alpha;ctx.setLineDash(dash.map(d=>d*devicePixelRatio));ctx.beginPath();points.forEach((p,i)=>{const q=window.project(p,z);i?ctx.lineTo(...q):ctx.moveTo(...q)});ctx.stroke();ctx.restore()}
function dot(p,z,color,r=4){const q=window.project(p,z);ctx.fillStyle=color;ctx.beginPath();ctx.arc(...q,r*devicePixelRatio,0,Math.PI*2);ctx.fill();return q}
function render(){const data=current();if(!data)return;const ratio=devicePixelRatio||1;const w=Math.max(1,Math.floor(canvas.clientWidth*ratio)),h=Math.max(1,Math.floor(canvas.clientHeight*ratio));if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h}ctx.clearRect(0,0,w,h);
const s=stage(data);window.project=s.point;const chosen=byId('uav').value,selected=chosen==='all'?Object.keys(data.uavs):[chosen];
const index=byId('returnIndex'),prior=index.value,keys=[...new Set(selected.flatMap(id=>data.uavs[id].return_keys))].sort((a,b)=>{const x=a.split(':').map(Number),y=b.split(':').map(Number);return x[0]-y[0]||x[1]-y[1]});fillSelect('returnIndex',[['none','不显示'],['all','全部返航线'],...keys.map(k=>{const [uid,n]=k.split(':');return [k,'UAV'+uid+' 第 '+n+' 航点']})],prior||keys[0]);
const polygon=data.boundary.map(p=>s.point(p));ctx.beginPath();polygon.forEach((q,i)=>i?ctx.lineTo(...q):ctx.moveTo(...q));ctx.closePath();ctx.fillStyle='rgba(95,137,180,.11)';ctx.fill();ctx.strokeStyle='#b4d4f2';ctx.lineWidth=2*ratio;ctx.stroke();
for(const ring of data.exclusions||[]){const coords=ring.map(p=>s.point(p));ctx.beginPath();coords.forEach((q,i)=>i?ctx.lineTo(...q):ctx.moveTo(...q));ctx.closePath();ctx.fillStyle='rgba(99,29,40,.72)';ctx.strokeStyle='#ff6172';ctx.lineWidth=2*ratio;ctx.fill();ctx.stroke();}
const pins=[];for(const id of selected){const u=data.uavs[id];if(!u)continue;const z=Number(s.hs[id]),col=colors[Number(id)-1];if(u.sector?.length){ctx.beginPath();u.sector.map(p=>s.point(p)).forEach((q,i)=>i?ctx.lineTo(...q):ctx.moveTo(...q));ctx.closePath();ctx.fillStyle=col+'28';ctx.fill();ctx.strokeStyle=col+'ab';ctx.stroke()}
path(u.entry,z,'#52c4ff',2.8,[],.95);path(u.scans,z,col,3.2,[],1);
const which=index.value;if(which==='all')u.returns.forEach(p=>path(p,z,'#ffa538',1.5,[5,4],.38));else if(which!=='none'){const i=u.return_keys.indexOf(which);if(i>=0)path(u.returns[i],z,'#ffa538',3.1,[7,4],1);}
u.scans.forEach((p,i)=>{const q=dot(p,z,col,4.4);pins.push({x:q[0],y:q[1],id,index:i+1,z})});
const home=u.home||data.departure;path([home,home],z,'#ef5350',2);const ground=s.point(home,0),top=s.point(home,z);ctx.strokeStyle='#ff6161';ctx.lineWidth=1.3*ratio;ctx.beginPath();ctx.moveTo(...ground);ctx.lineTo(...top);ctx.stroke();dot(home,0,'#ff5656',5.3);dot(home,z,'#ffd16a',3.2);
}
if(data.safe_boundary?.length)path([...data.safe_boundary,data.safe_boundary[0]],0,'#ffe086',2,[7,5],1);for(const ring of data.safe_exclusions||[])path([...ring,ring[0]],0,'#ffe086',2,[7,5],1);
const clearanceNote=byId('clearanceNote');clearanceNote.hidden=!(data.boundary_clearance_m>0);clearanceNote.textContent=data.boundary_clearance_m>0?'黄色虚线为内缩安全线：区域内侦察航点与航段距边界 ≥ '+data.boundary_clearance_m+'m；固定起降点靠边或在区外时，仅必要的起降连接段例外。':'';
if(hovered){const pin=pins.find(p=>p.id+'-'+p.index===hovered);if(pin){ctx.font=`${13*ratio}px Microsoft YaHei`;ctx.fillStyle='#fff';ctx.fillText('UAV'+pin.id+' · 航点'+pin.index+' · '+pin.z+'m',pin.x+8*ratio,pin.y-8*ratio)}}canvas._pins=pins;
const counts=selected.map(id=>'UAV'+id+'：'+data.uavs[id].scans.length+' 点，'+s.hs[id]+'m').join('<br>');byId('summary').textContent=names[byId('scene').value]+' · '+(live&&live.key===byId('scene').value+'/'+byId('departure').value?'实飞导入':'离线固定方案');byId('details').innerHTML='出发点：'+byId('departure').selectedOptions[0].textContent+'<br>坐标：局部北向 X / 西向 Y / 相对高度 Z<br>六机全部航点均返回选中飞机自己的降落点<br><br>'+counts;byId('legend').innerHTML=colors.map((c,i)=>'<span style="color:'+c+'">● UAV'+(i+1)+'</span>').join('');byId('homeNote').textContent=live&&live.key===byId('scene').value+'/'+byId('departure').value?'已导入本次规划：红色降落点、蓝色进场线和橙色返航线来自各机实测 GPS 后的方案。高度为相对起飞点高度。':'离线固定方案的降落点仅是示意。GPS 实飞规划先取得每架已连接无人机的有效经纬度，再以各机自身起飞点重接进场和逐航点返航路径。导入本次规划 JSON 可显示实飞方案。';}
function gpsToLocal(point,u,projection){const p=u.task.waypoints_m,g=u.task.waypoints_wgs84;let f=[1/projection.north_m_per_degree,-1/projection.west_m_per_degree];for(let a=0;a<2;a++){let lo=0,hi=0;for(let i=1;i<p.length;i++){if(p[i][a]<p[lo][a])lo=i;if(p[i][a]>p[hi][a])hi=i}if(p[hi][a]-p[lo][a]>1e-6)f[a]=(g[hi][a]-g[lo][a])/(p[hi][a]-p[lo][a])}return [p[0][0]+(point[1]-g[0][0])/f[0],p[0][1]+(point[0]-g[0][1])/f[1]]}
byId('import').addEventListener('change',async e=>{try{const f=e.target.files[0];if(!f)return;const doc=JSON.parse(await f.text()),mission=doc.mission||doc,area=mission.search_area,wrappers=mission.planned_uavs||doc.planned_uavs;if(!area||!wrappers)throw Error('缺少 search_area 或 planned_uavs');const key=area.flight_profile+'/'+area.departure_point,base=DATA.scenes[key];if(!base)throw Error('没有对应的固定场景');const modified=structuredClone(base);modified.heights={...DATA.heights[profileKey()]};for(const [id,wrapper] of Object.entries(wrappers)){const task=wrapper.task||wrapper,r=task.transit_routes;if(!r||!modified.uavs[id])continue;if(Number.isFinite(Number(wrapper.target_altitude_m)))modified.heights[id]=Number(wrapper.target_altitude_m);const projection=area.coverage.projection;const convert=r.coordinate_frame==='WGS84'?p=>gpsToLocal(p,wrapper,projection):p=>p;modified.uavs[id].entry=r.entry_path.map(convert);modified.uavs[id].returns=r.return_paths.map(x=>x.path.map(convert));modified.uavs[id].return_keys=r.return_paths.map(x=>String(x.source_uav_id??id)+':'+x.waypoint_index);modified.uavs[id].home=convert(r.departure)}live={key,data:modified};byId('scene').value=area.flight_profile;departures();byId('departure').value=area.departure_point;if(mission.flight_altitude_plan)byId('height').value=mission.flight_altitude_plan;render()}catch(error){alert('无法导入本次规划：'+error.message)}});
for(const id of ['scene','departure','height','uav','returnIndex','zScale'])byId(id).addEventListener('change',()=>id==='scene'?(live=null,departures()):render());byId('zScale').addEventListener('input',render);
byId('reset').onclick=()=>{angle=-.55;pitch=.80;zoom=1;render()};canvas.addEventListener('pointerdown',e=>{drag=true;last=[e.clientX,e.clientY];canvas.setPointerCapture(e.pointerId)});canvas.addEventListener('pointerup',()=>drag=false);canvas.addEventListener('pointermove',e=>{if(drag){angle+=(e.clientX-last[0])*.007;pitch=Math.max(.15,Math.min(1.48,pitch-(e.clientY-last[1])*.007));last=[e.clientX,e.clientY];render()}else{const x=e.offsetX*devicePixelRatio,y=e.offsetY*devicePixelRatio;const p=(canvas._pins||[]).find(p=>Math.hypot(p.x-x,p.y-y)<12*devicePixelRatio);const value=p?p.id+'-'+p.index:'';if(value!==hovered){hovered=value;byId('cursor').textContent=p?'UAV'+p.id+' 航点'+p.index+'，相对高度 '+p.z+'m':'将鼠标移至航点查看编号。';render()}}});canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.35,Math.min(8,zoom*Math.exp(-e.deltaY*.001)));render()},{passive:false});new ResizeObserver(render).observe(canvas);departures();
</script></body></html>'''


def main():
    output = ROOT / "docs/competition_plans_3d.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(build_data(), ensure_ascii=False, separators=(",", ":"))
    output.write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    print("已生成：{}（{} 字节）".format(output, output.stat().st_size))


if __name__ == "__main__":
    main()
