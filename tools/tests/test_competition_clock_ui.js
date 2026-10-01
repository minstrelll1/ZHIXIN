const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.resolve(__dirname,'../../competition_backend/competition_backend/web/index.html'),'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const source=html.slice(html.indexOf('let 比赛时钟='),html.indexOf('async function 选择地面终端('));
const nodes=new Map(),calls=[];let now=0;
const context={performance:{now:()=>now},setInterval:()=>{},crypto:{randomUUID:()=> 'unique'},
  当前角色:{configured:true,task_publisher:false},提示:()=>{},
  $:id=>{if(!nodes.has(id))nodes.set(id,{dataset:{},showModal(){this.open=true},close(){this.open=false}});return nodes.get(id)},
  请求:async(url,options)=>{const payload=JSON.parse(options.body);calls.push(payload);return {running:true,session_id:'s',revision:2,elapsed_seconds:payload.elapsed_seconds,updated_by:2,synchronized:true}}};
vm.createContext(context);vm.runInContext(source,context);
const sample={running:true,session_id:'s',revision:1,elapsed_seconds:87,updated_by:1,synchronized:true};
(async()=>{
  context.渲染比赛计时(sample);assert.equal(nodes.get('competitionClock').textContent,'00:01:27');
  assert.equal(nodes.get('detailCompetitionClock').textContent,'00:01:27');
  assert.equal(nodes.get('editCompetitionClock').disabled,false);
  now=2000;context.更新比赛计时显示();assert.equal(nodes.get('competitionClock').textContent,'00:01:29');
  context.渲染比赛计时(sample);assert.equal(nodes.get('competitionClock').textContent,'00:01:29');
  nodes.get('editCompetitionClock').onclick();
  nodes.get('competitionClockMinutes').value='2';nodes.get('competitionClockSeconds').value='10';
  await nodes.get('saveCompetitionClock').onclick();
  assert.deepEqual(calls[0],{elapsed_seconds:130,session_id:'s',request_id:'unique'});
  assert.equal(nodes.get('detailCompetitionClock').textContent,'00:02:10');
  context.渲染比赛计时(sample);assert.equal(nodes.get('competitionClock').textContent,'00:02:10');
  now+=5000;context.更新比赛计时显示();assert.match(nodes.get('competitionClockSync').textContent,/同步中断/);
  context.渲染比赛计时({...sample,revision:3,elapsed_seconds:20});assert.equal(nodes.get('competitionClock').textContent,'00:00:20');
  context.渲染比赛计时({...sample,session_id:'new',revision:0,elapsed_seconds:0});
  context.渲染比赛计时({...sample,revision:4});assert.equal(nodes.get('competitionClock').textContent,'00:00:00');
  nodes.get('competitionClockSeconds').value='60';await nodes.get('saveCompetitionClock').onclick();assert.equal(calls.length,1);
  nodes.get('competitionClockSeconds').value='5';context.请求=async()=>{throw Error('请求超时')};
  await nodes.get('saveCompetitionClock').onclick();assert.equal(nodes.get('saveCompetitionClock').disabled,false);
  assert.match(nodes.get('competitionClockFeedback').textContent,/核对最新比赛时间/);
  const detail=html.slice(html.indexOf('<dialog id="uavDetailDialog"'),html.indexOf('<dialog id="trafficDialog"'));
  assert.match(detail,/id="detailCompetitionClock"/);assert.doesNotMatch(detail,/editCompetitionClock|competitionClockMinutes/);
  console.log('比赛计时 UI：持续计时、普通终端修改、版本顺序、断网提示、详情只读验证通过');
})().catch(error=>{console.error(error);process.exitCode=1});
