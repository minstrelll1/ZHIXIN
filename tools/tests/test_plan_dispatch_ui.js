const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
assert.match(html, /<option value="xuchang_small">许昌试飞场地（小）/);
assert.match(html, /<option value="fixed_xuchang" hidden>许昌固定起飞点<\/option>/);
const receiptContext=vm.createContext({});
vm.runInContext(html.split('\n').find(line=>line.startsWith('function 任务回执文字(')),receiptContext);
const receipt=receiptContext.任务回执文字;
const currentMission={mission_id:'new'},runtime={assignment_checksum:'new-sum'};
const cached={connected:true,task_assignment_acked:true,task_assignment_mission_id:'old',task_assignment_checksum:'old-sum'};
assert.equal(receipt(null,null,cached),'未分派');
assert.equal(receipt(currentMission,runtime,cached),'待本次确认');
assert.equal(receipt(currentMission,runtime,{...cached,task_assignment_mission_id:'new'}),'待本次确认');
assert.equal(receipt(currentMission,runtime,{...cached,task_assignment_mission_id:'new',task_assignment_checksum:'new-sum'}),'已确认');
assert.equal(receipt(currentMission,runtime,{...cached,connected:false}),'未连接');
const requestStart=html.indexOf('async function 请求('),requestEnd=html.indexOf('\nfunction ',requestStart);
const flowStart=html.indexOf('let 比赛覆盖方案='),flowEnd=html.indexOf("$('planningAreaMode').onchange",flowStart);
function fixture(fetch,recognitionSelection={category_count:3,category_ids:[0,7,15]}){
  const nodes=new Map(),notes=[];
  let backendFetch=fetch;
  const defaults={subject:'subject1',planningAreaMode:'competition',controllerMode:'external',flightProfile:'lab',flightAltitudePlan:'around2m',coordinateMode:'xyz',scoutRadius:'1',flightSpeed:'0.2',hoverScanSeconds:'10',duration:''};
  let finishedPlan=null;
  const jobFetch=async(url,options)=>{
    if(url==='/api/v1/plan/jobs/current')return finishedPlan?response({job_id:'job-1',request_id:'same'}):{ok:false,status:404,json:async()=>({detail:'没有作业'})};
    if(url==='/api/v1/plan/jobs/job-1')return response({job_id:'job-1',state:'succeeded',result:finishedPlan});
    const result=await backendFetch(url,options);
    if(url==='/api/v1/plan/jobs'&&result.ok){finishedPlan=await result.json();return response({job_id:'job-1',state:'running'});}
    return result;
  };
  const context={fetch:jobFetch,AbortController,setTimeout,clearTimeout,console,当前状态:{mission:null},当前角色:{task_publisher:true},文本:{错误:{}},
    $:id=>{if(!nodes.has(id))nodes.set(id,{value:defaults[id]??'',style:{},disabled:false});return nodes.get(id)},
    获取规划识别类别:()=>recognitionSelection,提示:message=>notes.push(message),读取机载GPS参考组:()=>({}),数值:Number,渲染:()=>{}};
  vm.createContext(context);
  vm.runInContext(html.slice(requestStart,requestEnd)+'\n'+html.slice(flowStart,flowEnd),context);
  return {context,nodes,notes,setFetch:next=>{backendFetch=next},run:code=>vm.runInContext(code,context)};
}
const response=body=>({ok:true,json:async()=>body});
const mission=(ids=[])=>({mission:{mission_id:'new-plan',phase:'planned',uavs:Object.fromEntries(ids.map(id=>[id,{}]))},dispatch_status:{assigned_uav_ids:ids,acknowledged_uav_ids:[]}});
const watchdog=setTimeout(()=>{console.error('规划界面测试未能在限定时间内完成');process.exit(1)},15000);
(async()=>{
  // Server rejection: retry becomes available; no obsolete mission can be taken off.
  let count=0;
  const rejected=fixture(async url=>{if(url==='/api/v1/plan/jobs')count++;return {ok:false,status:409,json:async()=>({detail:'固定方案需在赛前重新生成'})}});
  await rejected.context.生成比赛覆盖();
  assert.equal(rejected.nodes.get('planButton').disabled,false);
  assert.equal(rejected.nodes.get('prepareButton').disabled,true);
  assert.match(rejected.nodes.get('dispatchFeedback').textContent,/固定方案需在赛前重新生成/);
  assert.equal(count,1);
  rejected.context.当前状态=mission([1]);rejected.context.更新任务按钮();
  assert.equal(rejected.nodes.get('prepareButton').disabled,true);
  rejected.setFetch(async()=>response(mission([1])));
  await rejected.context.生成比赛覆盖();
  assert.equal(rejected.nodes.get('prepareButton').disabled,false);
  // Confirmations refer to this mission only; unrelated snapshots cannot overwrite feedback.
  rejected.context.更新分派回执({...mission([1]),dispatch_status:{assigned_uav_ids:[1],acknowledged_uav_ids:[1]}});
  assert.match(rejected.nodes.get('dispatchFeedback').textContent,/全部确认/);
  rejected.context.更新分派回执({mission:{mission_id:'old-plan'},dispatch_status:{assigned_uav_ids:[]}});
  assert.match(rejected.nodes.get('dispatchFeedback').textContent,/全部确认/);
  // A rendering exception after successful assignment must not cause another POST or a stuck lock.
  const brokenMap=fixture(async()=>response(mission([1])));
  brokenMap.context.渲染=()=>{throw new Error('绘图异常')};
  await brokenMap.context.生成比赛覆盖();
  assert.equal(brokenMap.nodes.get('planButton').disabled,false);
  assert.match(brokenMap.nodes.get('dispatchFeedback').textContent,/后端已返回规划结果.*页面显示失败/);
  // A newer explicit click replaces pending work; only its result updates the page.
  const finishes=[];let calls=0;
  const pending=fixture(()=>{calls++;return new Promise(resolve=>{finishes.push(resolve)})});
  const first=pending.context.生成比赛覆盖();const second=pending.context.生成比赛覆盖();
  assert.equal(calls,2);assert.equal(pending.nodes.get('planButton').disabled,false);assert.equal(pending.nodes.get('prepareButton').disabled,true);
  finishes[0](response(mission([1])));await first;
  assert.equal(pending.nodes.get('prepareButton').disabled,true);
  finishes[1](response(mission()));await second;
  assert.equal(pending.nodes.get('planButton').disabled,false);
  assert.match(pending.nodes.get('dispatchFeedback').textContent,/未连接/);
  let zeroBody;
  const zero=fixture(async(_url,options)=>{zeroBody=JSON.parse(options.body);return response(mission())},{category_count:0,category_ids:[]});
  await zero.context.生成比赛覆盖();
  assert.equal(zeroBody.recognition_selection.category_count,0);
  assert.deepEqual(zeroBody.recognition_selection.category_ids,[]);
  assert.equal(zeroBody.require_recognition_selection,undefined);
  assert.equal(zero.nodes.get('planButton').disabled,false);
  // Subject 3 shares the same recoverable request lifecycle without adding competition parameters.
  let body;
  const subject3=fixture(async(_url,options)=>{body=JSON.parse(options.body);return response(mission([1]))});
  subject3.context.$('subject').value='subject3';await subject3.context.生成比赛覆盖();
  assert.equal(body.subject,'subject3');assert.equal(body.planning_mode,undefined);
  // Active mission and configuration restrictions are never unlocked by cleanup.
  for(const phase of ['taking_off','running','returning']){
    pending.context.当前状态={mission:{phase}};pending.context.更新任务按钮();
    assert.equal(pending.nodes.get('planButton').disabled,true);assert.equal(pending.nodes.get('prepareButton').disabled,true);
  }
  for(const snapshot of [{mission:null,operator:{selection_required:true,selection_pending:true}},{mission:null,fleet:{restart_required:true}}]){
    pending.context.当前状态=snapshot;pending.context.更新任务按钮();assert.equal(pending.nodes.get('planButton').disabled,true);
  }
  // Exercise the real timeout wrapper, including a stalled body read, without waiting 45 seconds.
  for(const stalledBody of [false,true]){
    const hanging=fixture((_url,{signal})=>{
      const wait=()=>new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));
      return stalledBody?Promise.resolve({ok:true,json:wait}):wait();
    });
    await assert.rejects(hanging.context.请求('/api/v1/plan',{timeoutMs:5}),/请求超时.*不会自动重发/);
  }
  const invalid=fixture(async()=>({ok:true,json:async()=>{throw new Error('bad json')}}));
  await invalid.context.生成比赛覆盖();assert.equal(invalid.nodes.get('planButton').disabled,false);
  assert.match(invalid.nodes.get('dispatchFeedback').textContent,/bad json|无法解析/);
  process.stdout.write('规划失败、超时、显示异常、重复点击、离线预览、回执与飞行锁定检查通过。\n');
})().catch(error=>{console.error(error);process.exitCode=1}).finally(()=>clearTimeout(watchdog));
