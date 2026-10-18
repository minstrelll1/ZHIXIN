// Offline DOM/canvas verification of the generated time-line viewer and runtime data.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'docs/competition_plans_3d.html'),'utf8');
const embedded=id=>html.match(new RegExp('<script id="'+id+'" type="application/json">([\\s\\S]*?)</script>'))[1];
const data=JSON.parse(embedded('planning-data'));
const fixed=JSON.parse(fs.readFileSync(path.join(root,'competition_backend/competition_backend/final_scenes_prepared.json'),'utf8'));
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const elements=new Map(),draws=[];
const drawing=new Proxy({}, {get:(o,k)=>k in o?o[k]:(...args)=>{
  if(['moveTo','lineTo','arc','clearRect','fillRect'].includes(k)){
    assert.ok(args.every(Number.isFinite),k+' finite coordinates');draws.push(k);
  }
  if(k==='createPattern')return {};
},set:(o,k,v)=>(o[k]=v,true)});
const checks=Array.from({length:6},(_,i)=>({dataset:{uav:String(i+1)},checked:true}));
function node(id){
  if(!elements.has(id))elements.set(id,{value:'',checked:true,style:{},textContent:'',innerHTML:'',
    getContext:()=>drawing,getBoundingClientRect:()=>({width:1100,height:700,left:0,top:0}),
    querySelectorAll:()=>checks,addEventListener(){},setPointerCapture(){},click(){},toDataURL:()=>''});
  return elements.get(id);
}
node('planning-data').textContent=embedded('planning-data');node('return-data').textContent=embedded('return-data');
node('diskPlane').value='altitude';node('playSpeed').value='30';node('zScale').value='1';
const ctx=vm.createContext({document:{getElementById:node,createElement:type=>node(type)},
  window:{devicePixelRatio:1,addEventListener(){}},requestAnimationFrame(){},console});
vm.runInContext(script,ctx);
assert.equal(ctx.window.previewReady,true,node('error').textContent);
assert.equal(data.source_inputs_sha256,fixed.sha256);
for(let i=0;i<data.scenes.length;i++){
  ctx.setScene(i);const scene=data.scenes[i],runtime=fixed.scenes[scene.scene_id].plan;
  assert.equal(node('error').textContent,'');
  assert.doesNotMatch(node('metrics').innerHTML,/NaN|undefined/);
  assert.doesNotMatch(node('jointMetrics').innerHTML,/1380|硬上限/);
  assert.equal(ctx.window.previewDiagnostics().pointCount,scene.uavs.reduce((n,u)=>n+u.layers.reduce((n,l)=>n+l.waypoints_m.length,0),0));
  for(const u of scene.uavs){
    const t=runtime.planned_uavs[String(u.uav_id)].task,scans=u.segments.filter(s=>s.phase==='scan');
    assert.equal(scans.length,t.scan_visit_count);
    assert.ok(Math.abs(u.timing.mission_time_s-t.mission_time_s)<.02);
    for(let j=0;j<u.segments.length;j++){
      const s=u.segments[j];assert.ok(s.duration_s>0);assert.ok(Math.abs(s.end_s-s.start_s-s.duration_s)<1e-8);
      if(j){assert.deepEqual(s.from_nwz_m,u.segments[j-1].to_nwz_m);assert.equal(s.start_s,u.segments[j-1].end_s);}
    }
    for(const layer of u.layers){
      const ids=layer.waypoint_ids;
      assert.deepEqual(scans.filter(s=>s.layer_id===layer.layer_id).map(s=>s.waypoint_id),ids.concat([...ids].reverse()));
      assert.ok(scans.filter(s=>s.layer_id===layer.layer_id).every(s=>s.duration_s===layer.hover_scan_seconds));
      if(t.layers)assert.deepEqual(layer.waypoints_m,t.layers.find(l=>l.layer_id===layer.layer_id).waypoints_m);
    }
    if(t.layers){
      assert.equal(u.timing.mission_budget_seconds,960);
      const transition=u.segments.filter(s=>s.phase==='layer_transition');assert.equal(transition.length,1);
      assert.deepEqual(transition[0].from_nwz_m.slice(0,2),u.layers[0].waypoints_m[0]);
      assert.deepEqual(transition[0].to_nwz_m.slice(0,2),u.layers[1].waypoints_m[0]);
      assert.ok(u.segments.findIndex(s=>s.layer_id==='lower')>u.segments.findLastIndex(s=>s.layer_id==='upper'));
    }
  }
  node('viewTop').onclick();node('view3d').onclick();node('jumpReturn').onclick();
  if(scene.scene_id!=='final_scene_1')assert.match(node('returnEvent').textContent,/返航回升/);
  for(const layer of ['upper','lower']){
    node('upper').checked=layer==='upper';node('lower').checked=layer==='lower';ctx.invalidate();
    assert.ok(vm.runInContext('points.every(p=>p.l.layer_id===\"'+layer+'\")',ctx));
  }
  node('upper').checked=node('lower').checked=true;
  node('noUavs').onclick();assert.equal(ctx.window.previewDiagnostics().pointCount,0);
  node('allUavs').onclick();node('diskPlane').value='ground';node('zScale').value='2';node('zScale').onchange();
  node('play').onclick();ctx.animate(1000);ctx.animate(2000);assert.ok(ctx.window.previewDiagnostics().time>0);
  node('resetTime').onclick();assert.equal(ctx.window.previewDiagnostics().time,0);
  console.log(scene.scene_id+': exact runtime points, round-trip timeline, budgets, layers and return animation verified');
}
assert.ok(draws.length>100);
for(const relative of ['docs/planning_preview.html','docs/planning_results_v2/planning_results_v2/planning_preview.html']){
  const page=fs.readFileSync(path.join(root,relative),'utf8');
  assert.ok(page.includes(embedded('planning-data')));
}
console.log('Timeline preview checks passed (offline; no aircraft commands)');
