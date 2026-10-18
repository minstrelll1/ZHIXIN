// Offline DOM/canvas doubles: execute the shipped selectors and 3D renderer without a browser or network.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'competition_backend/competition_backend/web/index.html'),'utf8');
new vm.Script(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const profiles=['final_scene_1','final_scene_2','xuchang_small_2'];
const elements=new Map();
function element(id){
  if(!elements.has(id))elements.set(id,{value:'',dataset:{},options:[],disabled:false,querySelector:()=>({textContent:''})});
  return elements.get(id);
}
element('coordinateMode').value='xyz';element('subject').value='subject1';
const ground=vm.createContext({$:element,更新GPS参考:()=>{}});
const start=html.indexOf('const 决赛场景名称='),end=html.indexOf('function 切换坐标系()',start);
vm.runInContext(html.slice(start,end),ground);
for(const profile of profiles){
  assert.ok(html.includes(`option value="${profile}"`));
  element('flightProfile').value=profile;
  ground.应用飞行环境默认值();
  assert.equal(element('coordinateMode').value,'gps');
  assert.equal(element('departurePoint').value,profile==='xuchang_small_2'?'fixed_xuchang':'stadium_center');
  assert.equal(element('flightAltitudePlan').value,profile==='final_scene_1'?'final_fixed':'final_dual');
  assert.equal(element('flightAltitudePlan').disabled,true);
  assert.equal(element('subject').value,'subject2');assert.equal(element('controllerMode').value,'external');
  assert.equal(element('scoutRadius').value,'50');assert.equal(element('hoverScanSeconds').value,'50');
  assert.equal(element('duration').disabled,true);assert.equal(element('duration').value,'');
}
element('flightProfile').value='subject1_actual';ground.应用飞行环境默认值();
assert.equal(element('flightAltitudePlan').disabled,false);assert.equal(element('flightAltitudePlan').value,'default');
assert.equal(element('subject').disabled,false);assert.equal(element('hoverScanSeconds').value,'40');
const legacyProfiles=['lab','lab10','outdoor5','outdoor100','outdoor200','competition','dalian_nanshan','xuchang_small','subject1_actual','subject2_actual'];
assert.ok(html.includes('option value="ascending34m"'));
element('flightAltitudePlan').options=[{value:'ascending34m'}];
for(const profile of legacyProfiles){
  element('flightProfile').value=profile;element('flightAltitudePlan').value='ascending34m';
  ground.应用飞行环境默认值();
  assert.equal(element('flightAltitudePlan').disabled,false);
  assert.equal(element('flightAltitudePlan').options[0].disabled,false);
  assert.equal(element('flightAltitudePlan').value,'ascending34m');
}

// Execute the ground map against the actual compiled polygons and both layers.
const prepared=JSON.parse(fs.readFileSync(path.join(root,'competition_backend/competition_backend/final_scenes_prepared.json'),'utf8'));
const drawStart=html.indexOf('function 绘制规划(mission)'),drawEnd=html.indexOf('let 点云状态=',drawStart);
const summaryStart=html.indexOf('function 航点说明(task)'),summaryEnd=html.indexOf('function 任务文字(',summaryStart);
for(const profile of profiles){
  const nodes=new Map(),ctx=new Proxy({}, {get:(o,k)=>k in o?o[k]:(...args)=>{
    if(['moveTo','lineTo','arc','fillRect','strokeRect','rect'].includes(k))assert.ok(args.every(Number.isFinite),`${profile} map ${k}`);
    if(k==='measureText')return {width:30};
  },set:(o,k,v)=>(o[k]=v,true)});
  function node(id){if(!nodes.has(id))nodes.set(id,{style:{},classList:{add(){},remove(){}},innerHTML:'',textContent:'',
    getBoundingClientRect:()=>({width:1000,height:600}),getContext:()=>ctx});return nodes.get(id);}
  const state=vm.createContext({$:node,window:{devicePixelRatio:1},document:{documentElement:{style:{setProperty(){}}}},
    当前状态:{active_uav_ids:[1,2,3,4,5,6],telemetry:{}},最近绘图任务:null,固定规划绘制键:'',地形显示:false,
    调整规划总览布局(){},同步详情规划图(){},规划图遥测:()=>null,实际轨迹:{byUav:{}},
    航线颜色:['blue','green','orange','purple','cyan','red'],决赛场景名称:{final_scene_1:'决赛场景1',final_scene_2:'决赛场景2',xuchang_small_2:'许昌试飞小场景2'}});
  vm.runInContext(html.slice(summaryStart,summaryEnd)+html.slice(drawStart,drawEnd),state);
  state.绘制规划(prepared.scenes[profile].plan);
  assert.doesNotMatch(node('assignmentList').innerHTML,/undefined|NaN/);
  assert.equal((node('assignmentList').innerHTML.match(/<li>/g)||[]).length,6);
  assert.match(node('landingPlan').textContent,/无任务时限/);
  if(profile!=='final_scene_1')assert.match(node('assignmentList').innerHTML,/上层 47米.*下层 34米/);
}

const report=fs.readFileSync(path.join(root,'docs/competition_plans_3d.html'),'utf8');
const data=report.match(/<script id="planning-data" type="application\/json">([\s\S]*?)<\/script>/)[1];
const script=report.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
async function checkReports(){
for(const profile of profiles){
  const nodes=new Map(),draws=[];
  const canvasContext=new Proxy({}, {get:(o,k)=> k in o?o[k]:(...args)=>{if(['moveTo','lineTo','arc'].includes(k)){assert.ok(args.every(Number.isFinite),`${profile}: nonfinite ${k}`);draws.push([k,...args]);}},set:(o,k,v)=>(o[k]=v,true)});
  function node(id){
    if(!nodes.has(id)){
      let content='',value='';const e={dataset:{},options:[],clientWidth:1100,clientHeight:650,width:0,height:0,disabled:false,listeners:{},
        addEventListener(name,callback){this.listeners[name]=callback;},getContext:()=>canvasContext,append(o){this.options.push(o);if(this.options.length===1)value=o.value;}};
      Object.defineProperties(e,{value:{get:()=>value,set:v=>{value=v;}},innerHTML:{get:()=>content,set:v=>{content=v;e.options=[];}},selectedOptions:{get:()=>e.options.filter(o=>o.value===value)}});
      nodes.set(id,e);
    }
    return nodes.get(id);
  }
  node('planning-data').textContent=data;node('zScale').value='35';node('layer').value='all';
  const context=vm.createContext({document:{getElementById:node,createElement:()=>({})},window:{},devicePixelRatio:1,
    URLSearchParams,location:{search:'?scene='+profile},ResizeObserver:class{observe(){}},structuredClone,console,alert(message){throw Error(message);}});
  vm.runInContext(script,context);
  assert.match(node('details').innerHTML,/无任务时限/);assert.match(node('details').innerHTML,/50m/);
  const key=profile+'/'+(profile==='xuchang_small_2'?'fixed_xuchang':'stadium_center');
  const expected=JSON.parse(data).scenes[key];
  assert.equal(node('height').disabled,true);
  const expectedCount=Object.values(expected.uavs).reduce((n,u)=>n+(u.layers.length?u.layers.reduce((s,l)=>s+l.waypoints_m.length,0):u.scans.length),0);
  assert.equal(node('view')._pins.length,expectedCount);
  for(const layer of ['upper','lower']){
    node('layer').value=layer;context.render();
    assert.ok(node('view')._pins.length>0);assert.ok(node('view')._pins.every(p=>p.layer===layer));
  }
  node('uav').value='4';node('layer').value='all';node('returnIndex').value='all';context.render();
  assert.ok(node('view')._pins.every(p=>p.id==='4'));assert.ok(draws.length>20);
  const actual=fs.readFileSync(path.join(root,'docs/final_scenes_program_b_examples',profile,'planning_preview.json'),'utf8');
  node('uav').value='all';
  await node('import').listeners.change({target:{files:[{text:async()=>actual}]}});
  assert.match(node('summary').textContent,/实飞导入/);
  assert.equal(node('height').value,'default');assert.equal(node('height').disabled,true);
  assert.equal(node('view')._pins.length,expectedCount);
  const imported=JSON.parse(actual).mission;
  for(const [uid,uav] of Object.entries(imported.planned_uavs)){
    const shown=context.current().uavs[uid],p=imported.search_area.coverage.projection,g=uav.task.transit_routes.departure;
    assert.ok(Math.abs(shown.home[0]-(g[1]-p.latitude)*p.north_m_per_degree)<.001);
    assert.ok(Math.abs(shown.home[1]+(g[0]-p.longitude)*p.west_m_per_degree)<.001);
    assert.equal(context.current().heights[uid],uav.target_altitude_m);
  }
  console.log(profile,'3D renderer: finite geometry, correct heights/layers/counts and return selection');
  if(profile==='final_scene_1')for(const legacy of legacyProfiles){
    node('scene').value=legacy;context.departures();
    assert.equal(node('height').disabled,false);
    assert.ok(node('height').options.some(o=>o.value==='ascending34m'));
    node('height').value='ascending34m';context.render();
    assert.ok(node('view')._pins.length>0);
    assert.ok(node('view')._pins.every(p=>p.z===[34,37,40,47,50,53][Number(p.id)-1]));
  }
}
}
checkReports().then(()=>console.log('Final scene selectors, maps and 3D import checks passed')).catch(error=>{console.error(error);process.exitCode=1;});
