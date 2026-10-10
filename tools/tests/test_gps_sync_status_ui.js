const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
const select=html.slice(html.indexOf('function 读取机载GPS参考组('),html.indexOf('function 读取机载GPS参考()'));
const sync=html.slice(html.indexOf('function GPS状态文本('),html.indexOf('const 六机状态观察器='));
const mode={value:'gps'},cards=[1,4].map(id=>({
  dataset:{uavId:String(id)},line:null,appendCount:0,
  querySelector(){return this.line},
  appendChild(line){this.line=line;this.appendCount++}
}));
const context={
  $:()=>mode,配置同步文本:snapshot=>snapshot.syncText||'配置已同步',
  document:{querySelectorAll:()=>cards,createElement:()=>({className:'',textContent:''})}
};
vm.createContext(context);vm.runInContext(select+'\n'+sync,context);
const fresh={connected:true,received_at:999.7,gps_telemetry_age_seconds:0.1,
  gps_status:3,location_source:4,latitude:34,longitude:113};
const snapshot=item=>({server_time:1000,live_mode:true,active_uav_ids:[1,4],telemetry:{'1':item,'4':fresh}});
const status=item=>context.GPS状态文本(snapshot(item),1);
assert.equal(status(fresh),'GPS：已获取');
for(const invalid of [
  {...fresh,connected:false},{...fresh,received_at:997.9},{...fresh,received_at:1000.1},
  {...fresh,received_at:null},{...fresh,gps_telemetry_age_seconds:1.8},
  {...fresh,gps_telemetry_age_seconds:-0.1},{...fresh,gps_telemetry_age_seconds:'invalid'},
  {...fresh,gps_status:undefined},{...fresh,gps_status:null},{...fresh,gps_status:2},
  {...fresh,location_source:2},{...fresh,latitude:null},{...fresh,longitude:181}
])assert.equal(status(invalid),'GPS：等待有效定位');
const precise={...fresh,latitude:null,longitude:null,gps_position:{latitude:34.123,longitude:113.456,age_seconds:0.2}};
assert.equal(status(precise),'GPS：已获取');
assert.equal(status({...precise,gps_position:{...precise.gps_position,age_seconds:1.8}}),'GPS：等待有效定位');
assert.equal(status({...fresh,gps_position:{latitude:91,longitude:113,age_seconds:0.1}}),'GPS：已获取');
const simulated=snapshot({...fresh,gps_status:0,location_source:-1});simulated.live_mode=false;
assert.equal(context.GPS状态文本(simulated,1),'GPS：已获取');
mode.value='xyz';assert.equal(status(fresh),'GPS：未启用');mode.value='gps';
// Both lines already exist on later snapshots: keep their nodes, update their status text.
context.更新六机同步状态(snapshot(fresh));
const original=cards[0].line;assert.match(original.textContent,/GPS：已获取/);
const stale=snapshot({...fresh,gps_telemetry_age_seconds:3});stale.syncText='配置同步失败';
context.更新六机同步状态(stale);
assert.equal(cards[0].line,original);assert.equal(cards[0].appendCount,1);
assert.match(cards[0].line.textContent,/配置同步失败 · GPS：等待有效定位/);
assert.match(cards[1].line.textContent,/GPS：已获取/);
context.更新六机同步状态(snapshot(fresh));
assert.match(cards[0].line.textContent,/配置已同步 · GPS：已获取/);
assert.equal(cards[0].appendCount,1);
console.log('GPS 同步状态：接收延迟与GPS年龄、定位标志、精度回退、模拟模式与已有状态行刷新检查通过。');
