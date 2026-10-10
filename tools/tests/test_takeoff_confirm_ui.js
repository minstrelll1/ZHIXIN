const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const texts=html.slice(html.indexOf('const 文本='),html.indexOf('let 当前状态='));
const escapeLine=html.split('\n').find(line=>line.startsWith('const 转义='));
const requestStart=html.indexOf('async function 请求(');
const request=html.slice(requestStart,html.indexOf('\nfunction ',requestStart));
const preflight=html.split('\n').find(line=>line.startsWith('function 渲染预检('));
const handlers=html.slice(html.indexOf('let 起飞预检提交中='),html.indexOf('function 打开返航选择('));
const buttons=html.slice(html.indexOf('function 更新任务按钮('),html.indexOf('function 显示分派反馈('));
const response=(body,status=200)=>({ok:status<400,status,json:async()=>body});
const ready=token=>({ready:true,confirmation_token:token,preflight:{ok:true,failures:{'1':[],'4':[]}}});
function setup(fetch){
  const nodes=new Map(),calls=[],notes=[];
  const context={
    确认令牌:null,当前状态:{mission:{mission_id:'mission-1',phase:'planned',uavs:{'1':{uav_id:1,target_altitude_m:10},'4':{uav_id:4,target_altitude_m:14}}}},
    当前角色:{task_publisher:true},覆盖规划中:false,规划结果待核对:false,AbortController,setTimeout,clearTimeout,
    $:id=>{if(!nodes.has(id))nodes.set(id,{disabled:false,open:false,innerHTML:'',showModal(){this.open=true},close(){this.open=false;this.onclose?.()}});return nodes.get(id)},
    提示:(message,error)=>notes.push({message,error}),渲染:()=>{},
    fetch:async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return fetch(url,options)}
  };
  vm.createContext(context);
  vm.runInContext([texts,escapeLine,request,preflight,handlers,buttons].join('\n'),context);
  return {context,nodes,calls,notes,prepare:()=>context.$('prepareButton').onclick(),confirm:()=>context.$('confirmTakeoff').onclick()};
}
function closed(fixture){
  assert.equal(fixture.context.确认令牌,null);
  assert.equal(fixture.nodes.get('takeoffDialog').open,false);
  assert.equal(fixture.nodes.get('confirmTakeoff').disabled,true);
}
(async()=>{
  // Backend invalidates the first token: the UI closes and needs another explicit prepare.
  let preparations=0,confirmations=0;
  const retry=setup(async url=>{
    if(url.endsWith('/prepare'))return response(ready('token-'+(++preparations)));
    return ++confirmations===1
      ?response({detail:'preflight changed before confirmation'},409)
      :response({mission:{phase:'taking_off'}});
  });
  await retry.prepare();assert.equal(retry.context.确认令牌,'token-1');
  await retry.confirm();closed(retry);
  assert.match(retry.nodes.get('checkList').innerHTML,/确认前飞行状态发生变化/);
  assert.match(retry.nodes.get('checkList').innerHTML,/重新执行起飞预检/);
  assert.doesNotMatch(retry.nodes.get('checkList').innerHTML,/检查通过/);
  await retry.confirm();
  assert.equal(confirmations,1);assert.equal(preparations,1);
  await retry.prepare();assert.equal(retry.context.确认令牌,'token-2');
  await retry.confirm();closed(retry);
  assert.deepEqual(retry.calls.map(call=>[call.url,call.body]),[
    ['/api/v1/takeoff/prepare',{}],['/api/v1/takeoff/confirm',{token:'token-1'}],
    ['/api/v1/takeoff/prepare',{}],['/api/v1/takeoff/confirm',{token:'token-2'}]
  ]);
  // Structured 409 details survive the actual request wrapper and show translated UAV failures.
  for(const nested of [true,false]){
    const report={ok:false,failures:{'1':['telemetry is stale'],'4':['task assignment is not acknowledged'],'system':['<script>bad()</script>']}};
    const failure=nested?{detail:{message:'preflight changed before confirmation',preflight:report}}:{detail:'preflight changed before confirmation',preflight:report};
    const fixture=setup(async url=>url.endsWith('/prepare')?response(ready('structured')):response(failure,409));
    await fixture.prepare();await fixture.confirm();closed(fixture);
    const checks=fixture.nodes.get('checkList').innerHTML;
    assert.match(checks,/无人机 1：遥测数据已超时/);
    assert.match(checks,/无人机 4：无人机尚未确认收到子任务/);
    assert.match(checks,/&lt;script&gt;bad\(\)&lt;\/script&gt;/);
    assert.doesNotMatch(checks,/<script>|\[object Object\]|检查通过/);
    assert.equal(fixture.calls.length,2);
  }
  // A lost response is ambiguous: discard the token and require state verification, never resend.
  const lost=setup(async url=>{if(url.endsWith('/prepare'))return response(ready('lost'));throw new Error('连接中断')});
  await lost.prepare();await lost.confirm();closed(lost);
  assert.match(lost.nodes.get('checkList').innerHTML,/先核对无人机任务状态/);
  await lost.confirm();assert.equal(lost.calls.length,2);
  // A second click while confirmation is pending cannot send the same token again.
  let release;
  const pending=setup(async url=>url.endsWith('/prepare')?response(ready('pending')):new Promise(resolve=>{release=resolve}));
  await pending.prepare();
  const first=pending.confirm();await pending.confirm();await pending.prepare();
  assert.equal(pending.calls.length,2);assert.equal(pending.context.确认令牌,null);
  release(response({detail:'takeoff confirmation token is invalid or expired'},409));await first;closed(pending);
  // A repeated prepare click also waits for its first request, preserving token order.
  let finishPrepare;
  const preparing=setup(()=>new Promise(resolve=>{finishPrepare=resolve}));
  const firstPrepare=preparing.prepare();await preparing.prepare();await preparing.confirm();
  assert.equal(preparing.calls.length,1);
  finishPrepare(response(ready('single-prepare')));await firstPrepare;
  assert.equal(preparing.context.确认令牌,'single-prepare');
  assert.equal(preparing.context.$('takeoffDialog').open,true);
  // Cancellation (including Escape's dialog close) needs another deliberate preparation.
  const cancel=setup(async()=>response(ready('cancel')));
  await cancel.prepare();cancel.context.$('cancelTakeoff').onclick();closed(cancel);
  assert.equal(cancel.calls.length,1);
  cancel.context.当前状态.mission.phase='preflight_ready';cancel.context.更新任务按钮();
  assert.equal(cancel.context.$('prepareButton').disabled,false);
  await cancel.prepare();cancel.context.$('takeoffDialog').close();closed(cancel);
  assert.equal(cancel.calls.length,2);
  // Failed re-preflight cannot leave an older successful token usable.
  let checks=0;
  const recheck=setup(async()=>response(++checks===1?ready('old'):{ready:false,preflight:{ok:false,failures:{'1':['telemetry is stale']}}}));
  await recheck.prepare();await recheck.prepare();closed(recheck);await recheck.confirm();
  assert.equal(recheck.calls.length,2);
  // A successful command followed by a rendering error must still be reported as sent.
  const renderFailure=setup(async url=>url.endsWith('/prepare')?response(ready('sent')):response({mission:{phase:'taking_off'}}));
  renderFailure.context.渲染=()=>{throw new Error('绘图异常')};
  await renderFailure.prepare();await renderFailure.confirm();closed(renderFailure);
  assert.match(renderFailure.nodes.get('checkList').innerHTML,/起飞命令已下发，但页面显示失败/);
  assert.doesNotMatch(renderFailure.nodes.get('checkList').innerHTML,/重新执行起飞预检/);
  await renderFailure.confirm();assert.equal(renderFailure.calls.length,2);
  // Allow only planned/preflight_ready manual retries, preserving flight and role safeguards.
  for(const phase of ['planned','preflight_ready','taking_off','running','returning','completed','failed']){
    cancel.context.当前状态={mission:{phase}};cancel.context.更新任务按钮();
    assert.equal(cancel.context.$('prepareButton').disabled,!['planned','preflight_ready'].includes(phase));
  }
  for(const snapshot of [
    {mission:{phase:'preflight_ready'},mission_read_only:true},
    {mission:{phase:'preflight_ready'},fleet:{restart_required:true}},
    {mission:{phase:'preflight_ready'},operator:{selection_required:true,selection_pending:true}}
  ]){
    cancel.context.当前状态=snapshot;cancel.context.更新任务按钮();
    assert.equal(cancel.context.$('prepareButton').disabled,true);
  }
  console.log('起飞确认界面：失效令牌清理、逐机错误、人工重新预检、取消恢复、并发防重复与飞行锁定检查通过。');
})().catch(error=>{console.error(error);process.exitCode=1});
