const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'competition_backend/competition_backend/web/index.html'), 'utf8');
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  if (script[1].trim()) new vm.Script(script[1]);
}
const start = html.indexOf('function 规划图遥测(');
const end = html.indexOf('function 调整规划总览布局(', start);
assert.ok(start >= 0 && end > start);
const context = {实际轨迹: {missionId: null, byUav: {}}};
vm.createContext(context);
vm.runInContext(html.slice(start, end), context);
const convert = context.规划图遥测;
const lat0 = 38.88025665283203, lon0 = 121.52688598632812;
const north = 110926, west = 92535;
const mission = {mission_id:'gps-A', phase:'running', search_area:{
  coordinate_mode:'gps', coordinate_frame:'WGS84', flight_profile:'outdoor5',
  gps_origin:{latitude:lat0, longitude:lon0},
  coverage:{projection:{latitude:33.86, longitude:113.71, north_m_per_degree:north, west_m_per_degree:west}}
}};
const item = {connected:true, received_at:100, location_source:5, gps_status:6,
  latitude:lat0 + 4.12/north, longitude:lon0 - 1.05/west,
  rel_alt:1.5, position:[-0.6,3.85,1.9], velocity:[1,2,3]};
const near = (actual, expected, tolerance=1e-6) => assert.ok(Math.abs(actual-expected)<tolerance, `${actual} != ${expected}`);

let mapped = convert(mission,item,100);
near(mapped.position[0],4.12); near(mapped.position[1],1.05);
assert.equal(mapped.position[2],1.5);
assert.deepEqual(Array.from(mapped.velocity),[2,-1,3]);
assert.equal(mapped.source,'prometheus_gps');

const precise = {...item, latitude:lat0, longitude:lon0, gps_position:{
  latitude:lat0 + .005/north, longitude:lon0 - .008/west, age_seconds:.1, source:'mavros_global'
}};
mapped=convert(mission,precise,100);
near(mapped.position[0],.005); near(mapped.position[1],.008);
assert.equal(mapped.source,'mavros_global');
assert.equal(convert(mission,{...precise,gps_position:{...precise.gps_position,age_seconds:2.1}},100).source,'prometheus_gps');

const xyz = {search_area:{coordinate_mode:'xyz',coordinate_frame:'ENU'}};
mapped=convert(xyz,item,100);
assert.deepEqual(Array.from(mapped.position),item.position);
assert.deepEqual(Array.from(mapped.velocity),item.velocity);

for (const latitude of [null,undefined,'',NaN,Infinity,91]) {
  assert.equal(convert(mission,{...item,latitude},100),null);
}
assert.equal(convert(mission,{...item,longitude:181},100),null);
assert.equal(convert(mission,{...item,gps_status:2},100),null);
assert.equal(convert(mission,{...item,location_source:0},100),null);
assert.equal(convert(mission,item,103),null);
// 心跳或地面轮询仍正常，实际定位包却已停更时，两种 GPS 都不可显示。
assert.equal(convert(mission,{...precise,gps_telemetry_age_seconds:2.1},100),null);
assert.equal(convert(mission,{...item,gps_telemetry_age_seconds:2.1},100),null);
assert.equal(convert(mission,{...item,gps_telemetry_age_seconds:1.9},100.2),null);
assert.ok(convert(mission,{...item,gps_telemetry_age_seconds:2},100));

assert.equal(convert({search_area:{coordinate_mode:'gps'}},item,100),null);
assert.equal(convert(mission,{...item,connected:false},100),null);

const absolute = {search_area:{coordinate_mode:'gps',coverage:{projection:mission.search_area.coverage.projection}}};
const onAbsoluteMap = {...item,latitude:33.86+4.12/north,longitude:113.71-1.05/west};
near(convert(absolute,onAbsoluteMap,100).position[0],4.12);
near(convert(absolute,onAbsoluteMap,100).position[1],1.05);

// 大连固定 WGS84 场地以起飞点为原点，北为上、西为左，且画布包含边界外起飞点。
const dalian = JSON.parse(fs.readFileSync(path.join(root,
  'competition_backend/competition_backend/dalian_nanshan_prepared.json'),'utf8')).plan;
const dalianArea = dalian.search_area;
const dalianNorth = dalianArea.coverage.projection.north_m_per_degree;
const dalianWest = dalianArea.coverage.projection.west_m_per_degree;
const dalianOrigin = dalianArea.departure_point_wgs84;
const dalianTelemetry = {...item,
  latitude:dalianOrigin.latitude+100/dalianNorth,
  longitude:dalianOrigin.longitude-100/dalianWest};
const dalianMapped = convert(dalian,dalianTelemetry,100);
near(dalianMapped.position[0],100);
near(dalianMapped.position[1],100);
assert.ok(dalianArea.origin_x_m<=0 && dalianArea.origin_y_m<=0);
assert.ok(dalianArea.origin_x_m+dalianArea.height_m>=100);
assert.ok(dalianArea.origin_y_m+dalianArea.width_m>=100);
const mapX = west => dalianArea.origin_y_m+dalianArea.width_m-west;
const mapY = north => dalianArea.origin_x_m+dalianArea.height_m-north;
assert.ok(mapX(100)<mapX(0));
assert.ok(mapY(100)<mapY(0));

context.更新实际轨迹({mission,telemetry:{1:item},active_uav_ids:[1],server_time:100});
let point=context.实际轨迹.byUav[1][0];
near(point.x,4.12); near(point.y,1.05);
context.更新实际轨迹({mission,telemetry:{1:item},active_uav_ids:[1],server_time:100});
assert.equal(context.实际轨迹.byUav[1].length,1);
context.更新实际轨迹({mission:{...mission,mission_id:'gps-B'},telemetry:{1:{...item,latitude:null}},active_uav_ids:[1],server_time:100});
assert.equal(context.实际轨迹.missionId,'gps-B');
assert.equal(Object.keys(context.实际轨迹.byUav).length,0);

// 143503 记录的本地位置重置不应让 GPS 地图上的飞机瞬间跳回原点。
const beforeReset={...item, latitude:38.88029098510742, longitude:121.52688598632812,
                   position:[-.63674,3.72393,1.9]};
const afterReset={...beforeReset,position:[-.001445,.000241,1.9]};
const beforePoint=convert(mission,beforeReset,100).position;
const afterPoint=convert(mission,afterReset,100).position;
assert.deepEqual(Array.from(afterPoint),Array.from(beforePoint));
assert.ok(beforePoint[0]>3.5);

// 规划 GPS 原点固定：其他实时遥测变化不参与原点重定位。
near(convert(mission,item,100).position[0],4.12);
for(const text of ['mapped=规划图遥测(mission,item,当前状态?.server_time)',
                   'mapped=规划图遥测(mission,telemetry,当前状态.server_time)',
                   'velocity=mapped?.velocity']) assert.ok(html.includes(text));
// 实际执行画布绘制：旧 ENU 位置在图外，正确 GPS 定位点必须仍出现在航点处。
const circles=[], arrows=[];
const ctx=new Proxy({arc:(x,y,r)=>circles.push({x,y,r}),measureText:()=>({width:80})}, {
  get:(target,key)=>key in target?target[key]:(()=>{}),set:(target,key,value)=>{target[key]=value;return true;}
});
const elements={};
context.$=id=>elements[id]||(elements[id]={className:'',classList:{add(){},remove(){}},style:{},
  getBoundingClientRect:()=>({width:800,height:1000}),getContext:()=>ctx});
context.window={devicePixelRatio:1};
context.document={documentElement:{style:{setProperty(){}}}};
context.调整规划总览布局=()=>{};
context.同步详情规划图=()=>{};
context.绘制速度箭头=(...args)=>arrows.push(args);
context.固定规划绘制键=''; context.地形显示=false;
context.航线颜色=['#155eef'];
const drawStart=html.indexOf('function 绘制规划(mission)');
const drawEnd=html.indexOf('let 点云状态=',drawStart);
vm.runInContext(html.slice(drawStart,drawEnd),context);
const routeMission={...mission,planned_uavs:{1:{uav_id:1,target_altitude_m:1.5,
  task:{type:'lawnmower_search',bounds_m:{x_min:3.13,x_max:5,y_min:0,y_max:2.37},waypoints_m:[[4.12,1.05]]}}},
  search_area:{...mission.search_area,origin_x_m:0,origin_y_m:0,width_m:4.2,height_m:5}};
context.当前状态={mission:routeMission,active_uav_ids:[1],telemetry:{1:item},server_time:100};
context.绘制规划(routeMission);
const markers=circles.filter(p=>p.r===8);
assert.equal(markers.length,1);
const scale=Math.min((800-76)/4.2,(1000-76)/5),offsetX=(800-4.2*scale)/2,offsetY=(1000-5*scale)/2;
near(markers[0].x,offsetX+(4.2-1.05)*scale,1e-4);
near(markers[0].y,1000-offsetY-4.12*scale,1e-4);
assert.equal(arrows.length,1); near(arrows[0][3],90); near(arrows[0][4],-180);
process.stdout.write('GPS 地图坐标、速度、精度、无效定位、XYZ兼容和轨迹检查通过。\n');

// 正式科目一按附件固定地理参考显示，开机落点不平移侦察区域。
const actual = JSON.parse(fs.readFileSync(path.join(root,
  'competition_backend/competition_backend/subject1_actual_prepared.json'),'utf8')).plan;
const ap = actual.search_area.coverage.projection;
const scan = actual.planned_uavs['1'].task.waypoints_m[0];
const fix = {...item,latitude:ap.latitude+scan[0]/ap.north_m_per_degree,
  longitude:ap.longitude-scan[1]/ap.west_m_per_degree};
near(convert(actual,fix,100).position[0],scan[0]);
near(convert(actual,fix,100).position[1],scan[1]);
actual.search_area.takeoff_gps_by_uav={'1':{latitude:33.8645,longitude:113.7056}};
near(convert(actual,fix,100).position[0],scan[0]);
near(convert(actual,fix,100).position[1],scan[1]);
