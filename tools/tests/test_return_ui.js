const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const source=html.slice(html.indexOf('function 返航回执说明('),html.indexOf("$('startRecording').onclick"));
function setup(publisher=true){
  const elements=new Map(),calls=[];
  const context={当前状态:{mission:{mission_id:'m',controller_mode:'external',uavs:{'1':{},'2':{},'3':{phase:'landed'}}},telemetry:{'1':{connected:true}}},
    当前角色:{task_publisher:publisher,ground_terminal_id:2},
    $:id=>{if(!elements.has(id))elements.set(id,{dataset:{},style:{},showModal(){this.open=true},close(){this.open=false},querySelectorAll(selector){return selector==='input:checked'?(this.checked||[]).filter(b=>b.checked!==false):(this.checked||[])}});return elements.get(id)},
    请求:async(url,options)=>{calls.push([url,JSON.parse(options.body)]);return {...context.当前状态,return_results:{'1':{ok:true,detail:'已发送'},'2':{ok:false,detail:'链路断开'}}}},渲染:()=>{}};
  vm.createContext(context);vm.runInContext(source,context);
  return {context,elements,calls};
}
(async()=>{
  const {context,elements,calls}=setup();
  context.打开返航选择();
  assert.match(elements.get('returnChoices').innerHTML,/UAV1/);
  assert.match(elements.get('returnChoices').innerHTML,/value="3" disabled/);
  assert.match(elements.get('returnModeNote').textContent,/程序 B/);
  await elements.get('returnConfirm').onclick();assert.equal(calls.length,0);
  elements.get('returnChoices').checked=[{value:'1'},{value:'2'}];
  await elements.get('returnConfirm').onclick();
  assert.deepEqual(calls[0],['/api/v1/return',{reason:'manual',uav_ids:[1,2],mission_id:'m'}]);
  assert.match(elements.get('returnResults').textContent,/UAV2：发送失败/);
  const receipt=(state)=>({mission_id:'m',state,age_seconds:2,timeout_seconds:8});
  context.当前状态.return_requests={'1':receipt('forwarded'),'2':receipt('timeout'),'3':receipt('landed')};
  context.更新返航回执(context.当前状态);
  assert.match(elements.get('returnAck1').textContent,/机载已转交程序 B/);
  assert.match(elements.get('returnAck2').textContent,/回执超时/);
  assert.match(elements.get('returnResults').textContent,/UAV1[^\n]*\nUAV2/);
  elements.get('returnSelectUnconfirmed').onclick();
  assert.equal(elements.get('returnChoices').checked[0].checked,false);
  assert.equal(elements.get('returnChoices').checked[1].checked,true);
  await elements.get('returnConfirm').onclick();
  assert.deepEqual(calls[1][1].uav_ids,[2]);
  context.当前状态.return_requests['2']=receipt('rejected');
  context.更新返航回执(context.当前状态);
  assert.match(elements.get('returnAck2').textContent,/机载已拒绝/);
  context.当前状态.return_requests['2']=receipt('accepted');
  context.更新返航回执(context.当前状态);
  assert.match(elements.get('returnAck2').textContent,/机载已接收/);
  context.当前状态.return_requests['2'].mission_id='old';
  context.更新返航回执(context.当前状态);
  assert.match(elements.get('returnAck2').textContent,/尚未发送/);
  context.请求=async()=>{throw new Error('超时')};
  await elements.get('returnConfirm').onclick();
  assert.equal(elements.get('returnConfirm').disabled,false);
  assert.equal(elements.get('returnResults').textContent,'超时');
  context.当前状态.mission.mission_id='new';
  await elements.get('returnConfirm').onclick();
  assert.match(elements.get('returnResults').textContent,/任务已变化/);
  const follower=setup(false);follower.context.打开返航选择();
  const choices=follower.elements.get('returnChoices').innerHTML;
  assert.match(choices,/UAV2/);assert.doesNotMatch(choices,/UAV1|UAV3/);
  follower.context.当前状态.mission.controller_mode='internal';follower.context.打开返航选择();
  assert.match(follower.elements.get('returnModeNote').textContent,/本工程执行返航/);
  console.log('返航选择界面：角色限制、逐机回执、只重发未确认飞机、失败恢复、过期任务校验通过');
})().catch(error=>{console.error(error);process.exitCode=1});
