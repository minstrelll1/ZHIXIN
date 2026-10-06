/* Esri 历史影像示意底图。只影响绘图，不参与航点、GPS 原点或飞行控制计算。 */
(function(root){
  'use strict';
  const source = {"image_bounds": [[33.85714191084824, 113.69893692568316], [33.87263819104422, 113.71759918542799]], "points": [[33.86434444444445, 113.70741944444445], [33.864219444444444, 113.70589722222222], [33.864805555555556, 113.70581944444444], [33.86476944444445, 113.70532222222222], [33.86416666666667, 113.7054], [33.864025, 113.70383333333334], [33.86281388888889, 113.70373888888889], [33.86095, 113.70356666666667], [33.86063055555556, 113.70715], [33.862436111111116, 113.70729166666666], [33.86241388888889, 113.70743333333334], [33.86235555555556, 113.70861111111111], [33.86194166666667, 113.70856944444445], [33.86190555555556, 113.70981111111111], [33.860419444444446, 113.70973333333333], [33.860275, 113.71243611111112], [33.86180277777778, 113.71254722222223], [33.865500000000004, 113.71280833333334], [33.867647222222224, 113.71296944444445], [33.86756388888889, 113.71015833333334], [33.86801666666667, 113.71019722222222], [33.869505555555556, 113.71010555555556], [33.869261111111115, 113.70786944444444], [33.86573055555556, 113.70770555555556], [33.86436666666667, 113.707575]], "projection": {"latitude": 33.86407466666667, "longitude": 113.70815877777777, "method": "WGS84_local_midlatitude", "north_m_per_degree": 110919.93326612496, "west_m_per_degree": 92531.6750158963}};
  const mercator = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  const latitude = y => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;
  const bounds = points => ({minX:Math.min(...points.map(p=>p[0])), maxX:Math.max(...points.map(p=>p[0])),
    minY:Math.min(...points.map(p=>p[1])), maxY:Math.max(...points.map(p=>p[1]))});
  const project = (lon,lat) => [(lat-source.projection.latitude)*source.projection.north_m_per_degree,
    -(lon-source.projection.longitude)*source.projection.west_m_per_degree];
  const original = source.points.map(p=>project(p[1],p[0]));
  const extent = bounds(original);
  const state = {status:'idle',image:null,revision:0,attempt:0,serial:0,retryTimer:null,loadTimer:null};
  function displayArea(area){
    // 旧正式比赛 GPS 任务：航线已归零，边界仍在原投影坐标。
    // 只修正显示副本；不修改任务、GPS 基准或发送给机载端的数据。
    if(!area||area.display_translation||area.coordinate_mode!=='gps'||area.flight_profile!=='competition')return area;
    const points=area.points_m,projection=area.coverage?.projection;
    if(!Array.isArray(points)||points.length!==original.length||!projection||
       Math.abs(projection.latitude-source.projection.latitude)>1e-9||Math.abs(projection.longitude-source.projection.longitude)>1e-9)return area;
    if(points.some(p=>!Array.isArray(p)||!Number.isFinite(p[0])||!Number.isFinite(p[1])))return area;
    const b=bounds(points),dx=Number(area.origin_x_m)-b.minX,dy=Number(area.origin_y_m)-b.minY;
    if(!Number.isFinite(dx)||!Number.isFinite(dy)||Math.hypot(dx,dy)<1e-6)return area;
    if(Math.abs((b.maxX-b.minX)-area.height_m)>.01||Math.abs((b.maxY-b.minY)-area.width_m)>.01)return area;
    return {...area,points_m:points.map(p=>[p[0]+dx,p[1]+dy]),display_translation:[dx,dy]};
  }
  function displayPosition(area,p){
    const offset=displayArea(area)?.display_translation;
    return offset&&Array.isArray(p)?[p[0]+offset[0],p[1]+offset[1],...p.slice(2)]:p;
  }
  function mapping(area){
    area=displayArea(area);
    const points=area?.points_m||area?.coverage?.polygon_m;
    if(!Array.isArray(points)||points.length!==original.length||points.some(p=>!Array.isArray(p)||!Number.isFinite(p[0])||!Number.isFinite(p[1])))return null;
    const target=bounds(points),sx=(target.maxX-target.minX)/(extent.maxX-extent.minX),sy=(target.maxY-target.minY)/(extent.maxY-extent.minY);
    if(!(sx>0&&sy>0))return null;
    const local=p=>[target.minX+(p[0]-extent.minX)*sx,target.minY+(p[1]-extent.minY)*sy];
    // 仅允许同一比赛区域的平移、缩放，避免将这张影像套到另一块真实场地。
    const tolerance=Math.max(target.maxX-target.minX,target.maxY-target.minY)*0.00002+0.000002;
    if(original.some((p,i)=>Math.hypot(local(p)[0]-points[i][0],local(p)[1]-points[i][1])>tolerance))return null;
    return {local,geo:(lon,lat)=>local(project(lon,lat)),scaleX:sx,scaleY:sy};
  }
  function clearTimer(key){if(state[key]!==null){root.clearTimeout?.(state[key]);state[key]=null;}}
  function ensureImage(){
    if(['ready','loading'].includes(state.status)||typeof root.Image!=='function')return;
    clearTimer('retryTimer');
    state.status='loading';state.attempt++;state.revision++;
    const serial=++state.serial,image=new root.Image();state.image=image;
    const failed=()=>{
      if(serial!==state.serial||state.status!=='loading')return;
      clearTimer('loadTimer');state.status='error';state.revision++;
      // 同一次加载最多自动恢复三次，避免持续高频请求；再次规划可重新触发恢复。
      if(state.attempt<4&&root.setTimeout)state.retryTimer=root.setTimeout(()=>{state.retryTimer=null;ensureImage();},[1500,5000,15000][state.attempt-1]);
      api.onChange?.();
    };
    image.onload=()=>{
      if(serial!==state.serial||state.status!=='loading')return;
      if(!(image.naturalWidth>0&&image.naturalHeight>0)){failed();return;}
      clearTimer('loadTimer');state.status='ready';state.revision++;api.onChange?.();
    };
    image.onerror=failed;
    if(root.setTimeout)state.loadTimer=root.setTimeout(failed,8000);
    image.src='/map-assets/competition_esri_20170724.jpg'+(serial>1?'?retry='+serial:'');
  }
  function prepare(){
    if(state.status==='error'){clearTimer('retryTimer');state.status='idle';state.attempt=0;}
    ensureImage();
  }
  function caption(area){
    if(area?.flight_profile==='dalian_nanshan')return '大连南山坡外场尚无已校准卫星底图；任务边界、航点和实时 GPS 位置按固定 WGS84 坐标绘制';
    if(area?.flight_profile==='xuchang_small')return '许昌试飞场地（小）按固定 WGS84 坐标绘制；深灰色区域为内部禁飞扣除区';
    if(!mapping(area))return 'Esri 示意底图：当前区域形状不匹配';
    if(state.status==='error')return 'Esri 底图加载失败，请检查地面端地图资源';
    if(state.status!=='ready')return '正在加载本地 Esri 底图…';
    const scaled=['lab','outdoor5','lab10','outdoor100','outdoor200'].includes(area.flight_profile);
    return `Esri 历史影像 · 2017-07-24${scaled?' · 等比例缩小示意（非测试现场）':area.coordinate_mode==='xyz'?' · 比赛场地示意（非 XYZ 实景定位）':' · WGS84 比赛场地'} | Esri / Vantor / Earthstar Geographics / GIS User Community`;
  }
  function draw(ctx,area,px,py,clip){
    const transform=mapping(area);if(!transform)return false;
    if(state.status==='idle')ensureImage();if(state.status!=='ready')return false;
    const image=state.image,iw=image.naturalWidth,ih=image.naturalHeight;
    const [[south,west],[north,east]]=source.image_bounds;
    const top=mercator(north),bottom=mercator(south);
    ctx.save();ctx.beginPath();ctx.rect(clip.x,clip.y,clip.width,clip.height);ctx.clip();
    ctx.globalAlpha=1;ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';
    // 影像为 Web Mercator，规划为局部米坐标；分条转换纬度，避免把两种投影简单拉伸。
    for(let row=0;row<32;row++){
      const y0=row*ih/32,y1=(row+1)*ih/32;
      const nw=transform.geo(west,latitude(top+(bottom-top)*y0/ih));
      const se=transform.geo(east,latitude(top+(bottom-top)*y1/ih));
      const x=px(nw[1]),y=py(nw[0]),width=px(se[1])-x,height=py(se[0])-y;
      ctx.drawImage(image,0,y0,iw,y1-y0,x,y,width,height);
    }
    ctx.restore();return true;
  }
  function layerPoint(area,p){
    // 旧 GPS 缩小方案保留了原场地地类米坐标；显示时须应用与影像相同的缩放。
    if(area.coordinate_mode==='gps'&&['competition','lab','lab10','outdoor5'].includes(area.flight_profile))return mapping(area)?.local(p)||p;
    return p;
  }
  function fitRect(sw,sh,dw,dh){
    if(![sw,sh,dw,dh].every(v=>Number.isFinite(v)&&v>0))return null;
    const scale=Math.min(dw/sw,dh/sh),width=sw*scale,height=sh*scale;
    return {x:(dw-width)/2,y:(dh-height)/2,width,height};
  }
  const api={source,mapping,draw,caption,layerPoint,fitRect,displayArea,displayPosition,prepare,onChange:null,
    get revision(){return state.revision;},
    retry:prepare};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  root.CompetitionTerrain=api;
})(typeof window!=='undefined'?window:globalThis);
