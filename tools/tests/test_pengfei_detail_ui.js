const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
const start=html.indexOf('function 彭飞数字(');
const end=html.indexOf('function 渲染详情(',start);
assert.ok(start>=0&&end>start);
const root={innerHTML:''};
const context={
  $:()=>root,
  转义:value=>String(value??'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])),
};
vm.createContext(context);
vm.runInContext(html.slice(start,end),context);
const sample={
  connected:true,received_at:1000,pengfei_age_seconds:0.1,
  pengfei:{
    sent_at_unix:100,
    current_target:{received_at_unix:99.8,target_id:'<img src=x>',target_type:'车辆1',latitude_deg:30.785,longitude_deg:103.861,altitude_gps_m:null,velocity_north_mps:1.2,velocity_east_mps:-0.3},
    last_recognition:{received_at_unix:99.8,target_id:'target-1',target_type:'车辆1',latitude_deg:30.784,longitude_deg:103.860},
    node_status:{received_at_unix:99.8,scheduler_state:'tracking',maneuver_state:'inactive',gimbal_state:'tracking',follower_state:'inactive',scan_point_number:2},
  },
};
context.渲染彭飞数据(sample,1000.1);
assert.match(root.innerHTML,/&lt;img src=x&gt;/);
assert.doesNotMatch(root.innerHTML,/<img src=x>/);
assert.match(root.innerHTML,/30\.7850000°/);
assert.match(root.innerHTML,/估计 GPS 高度<\/span><strong>—/);
assert.match(root.innerHTML,/当前侦察点<\/span><strong>2/);
assert.match(root.innerHTML,/最近接收/);
assert.match(root.innerHTML,/可能是历史记录/);

context.渲染彭飞数据({...sample,pengfei_age_seconds:3},1000.1);
assert.match(root.innerHTML,/话题超时/);
assert.doesNotMatch(root.innerHTML,/&lt;img src=x&gt;/);
assert.doesNotMatch(root.innerHTML,/>tracking<\/strong>/);

context.渲染彭飞数据({...sample,connected:false},1000.1);
assert.match(root.innerHTML,/机地链路中断/);
assert.doesNotMatch(root.innerHTML,/&lt;img src=x&gt;/);

context.渲染彭飞数据({connected:true,received_at:1000,pengfei:{}},1000);
assert.match(root.innerHTML,/未收到话题/);
assert.ok(html.includes('渲染彭飞数据(telemetry,当前状态.server_time);'));
console.log('程序 B 任务详情显示、超时和空值检查通过。');
