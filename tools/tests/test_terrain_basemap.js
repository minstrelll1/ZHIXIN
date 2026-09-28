const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=process.env.COMPETITION_TEST_ROOT||path.resolve(__dirname,'../..');
const file=path.join(root,'competition_backend/competition_backend/web/terrain_basemap.js');
const code=fs.readFileSync(file,'utf8');
let requests=0,lastImage;
class Image {constructor(){lastImage=this;this.naturalWidth=1800;this.naturalHeight=1800;}set src(v){requests++;assert.equal(v,'/map-assets/competition_esri_20170724.jpg');}}
const context={Image};vm.createContext(context);vm.runInContext(code,context);
const api=context.CompetitionTerrain;
const original=JSON.parse(fs.readFileSync(path.join(root,'competition_backend/competition_backend/competition_coverage_default.json'),'utf8')).plan.search_area;
const close=(a,b,tol=1e-5)=>assert.ok(Math.abs(a-b)<tol,`${a} != ${b}`);
for(const [profile,size] of [['competition',null],['lab',3],['outdoor5',5],['lab10',10]]){
 for(const mode of ['gps','xyz']){
  const factor=size?size/Math.max(original.width_m,original.height_m):1;
  const shifted=mode==='xyz'||size;
  const points=original.points_m.map(p=>shifted?[(p[0]-original.origin_x_m)*factor,(p[1]-original.origin_y_m)*factor]:p);
  const area={...original,points_m:points,coordinate_mode:mode,flight_profile:profile};
  const transform=api.mapping(area);assert.ok(transform,`${profile}/${mode}`);
  api.source.points.forEach(([lat,lon],i)=>{
    const q=transform.geo(lon,lat);close(q[0],points[i][0]);close(q[1],points[i][1]);
  });
  if(size&&mode==='gps'){
    original.points_m.forEach((p,i)=>{const q=api.layerPoint(area,p);close(q[0],points[i][0]);close(q[1],points[i][1]);});
  }
 }
}
// 正式比赛 GPS 任务与赛前预览不同：航线归零，旧边界/地类却仍在原投影原点。
const runtime={...original,coordinate_mode:'gps',flight_profile:'competition',origin_x_m:0,origin_y_m:0};
const before=JSON.stringify(runtime),display=api.displayArea(runtime),transform=api.mapping(runtime);
assert.notEqual(display,runtime);
original.points_m.forEach((p,i)=>{
  const expected=[p[0]-original.origin_x_m,p[1]-original.origin_y_m];
  const q=display.points_m[i],position=api.displayPosition(runtime,[...p,1.5]);
  const geo=transform.geo(api.source.points[i][1],api.source.points[i][0]);
  const layer=api.layerPoint(display,p);
  for(let j=0;j<2;j++){close(q[j],expected[j]);close(position[j],expected[j]);close(geo[j],expected[j]);close(layer[j],expected[j]);}
  assert.equal(position[2],1.5);
});
assert.equal(JSON.stringify(runtime),before,'只修正显示，不修改发送给机载端的原始任务');
assert.equal(api.displayArea(display),display,'重复绘制不能重复平移');
assert.equal(api.mapping({...original,points_m:[[0,0],[1,0],[1,1],[0,1]]}),null);
assert.equal(api.mapping({...original,points_m:original.points_m.map((p,i)=>i===4?[p[0]+30,p[1]]:p)}),null);
const draws=[];
const ctx=new Proxy({drawImage:(...a)=>draws.push(a)},{get:(o,k)=>k in o?o[k]:(()=>{}),set:(o,k,v)=>(o[k]=v,true)});
const clip={x:20,y:30,width:500,height:600};
assert.equal(api.draw(ctx,original,y=>-y,x=>-x,clip),false);assert.equal(requests,1);
api.draw(ctx,original,y=>-y,x=>-x,clip);assert.equal(requests,1);
lastImage.onload();
assert.equal(api.draw(ctx,original,y=>-y,x=>-x,clip),true);assert.equal(draws.length,32);
assert.match(api.caption(original),/2017-07-24/);
for(let i=0;i<draws.length;i++){
 const d=draws[i];assert.ok(d[7]>0&&d[8]>0);
 if(i)close(draws[i-1][6]+draws[i-1][8],d[6]);
}
// 再次渲染/切换尺寸不重新请求影像；下载失败也不在状态轮询中反复请求。
api.draw(ctx,original,y=>-y*2,x=>-x*2,clip);assert.equal(requests,1);
lastImage.onerror();api.draw(ctx,original,y=>-y,x=>-x,clip);assert.equal(requests,1);
assert.match(api.caption(original),/加载失败/);api.retry();api.draw(ctx,original,y=>-y,x=>-x,clip);assert.equal(requests,2);
for(const dims of [[800,1000,1200,600],[800,1000,300,900]]){
 const fit=api.fitRect(...dims);close(fit.width/fit.height,dims[0]/dims[1]);
 assert.ok(fit.x>=0&&fit.y>=0&&fit.width<=dims[2]&&fit.height<=dims[3]);
}
assert.equal(api.fitRect(0,100,300,400),null);
console.log('Esri 底图：八种场景/坐标系、25 点配准、地类缩放、投影分条、缓存/失败恢复、详情等比显示检查通过。');
