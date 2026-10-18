#!/usr/bin/env python3
"""Deterministic offline multi-layer survey planner. No flight-control interfaces.
Run: python3 planner.py (paths default to this file's directory).
"""
import argparse, csv, hashlib, heapq, json, math, platform, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np
import shapely
from shapely.geometry import Point, Polygon, MultiPolygon, LineString, box, mapping, shape
from shapely.ops import unary_union, transform
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent
QUAD = 128
PARAM = dict(circle_quad_segs=QUAD, random_seed=20261017,
    candidate_grid_step_m=10., target_sample_step_m=4., clearance_generation_m=5.03,
    disk_clearance_extra_m=0.05, greedy_shortlist_per_layer=12, post_pruning_refill_rounds=3,
    minimum_sample_gain_ratio=0.001, marginal_time_exponent=0.6,
    layer_balance_offset=0.25,
    coverage_priority_budget_s=1350., reserve_priority_budget_s=1230., return_connection_policy='Shortest own-upper visible attachment plus recipient return gates; full all-source catalog also delivered',
    partition_candidates=['effective_area_balanced','gross_area_balanced'],
    termination='No shortlisted candidate with >=0.1% new sampled area (minimum 2 samples) fits budget; finite candidates; deterministic 2-opt until no improvement, max 8 sweeps. This is a heuristic, not proof of global optimality.',
    objective='Feasible first; greedy layer area fraction gain / (0.25+current_layer_coverage) / incremental_seconds^0.6 to avoid starving the small-radius layer; partition by largest summed per-layer field coverage, ties shortest makespan; route length and soft transition penalty.',
    altitude_status='accepted_provisional_not_measured', descent_status='accepted_provisional_not_measured', return_climb_status='planner_provisional_unconfirmed', direct_scan_arrays_required=True, redundancy_deletion_threshold_m2=0.01,
    coordinate_precision='No intentional rounding; full IEEE double JSON; revalidate serialized artifact.',
    geometry_model='Planar metres; 128 segments per circle quadrant; optimization samples 4m grid, final metrics exact polygon Boolean operations with 512-edge disks.')

def dump(path,obj):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')

def nw(lat,lon,p): return [(lat-p['latitude'])*p['north_m_per_degree'], -(lon-p['longitude'])*p['west_m_per_degree']]
def ll(q,p): return [p['latitude']+q[0]/p['north_m_per_degree'],p['longitude']-q[1]/p['west_m_per_degree']]
def geo(g,p):
    if g.is_empty: return None
    # Remove sub-micrometre Boolean slivers before coordinate conversion. This
    # preserves holes/components and avoids collapsed rings in serialized WGS84.
    g=shapely.set_precision(g,1e-7,mode='valid_output')
    out=transform(lambda x,y,z=None:(p['longitude']-np.asarray(y)/p['west_m_per_degree'],p['latitude']+np.asarray(x)/p['north_m_per_degree']),g)
    if not out.is_valid:out=polyonly(shapely.make_valid(out))
    return mapping(out) if not out.is_empty else None
def local(g,p):
    return transform(lambda lon,lat,z=None:((np.asarray(lat)-p['latitude'])*p['north_m_per_degree'],-(np.asarray(lon)-p['longitude'])*p['west_m_per_degree']),shape(g))
def polys(g): return [] if g.is_empty else [g] if g.geom_type=='Polygon' else list(g.geoms)
def polyonly(g):
    if g.geom_type in ['Polygon','MultiPolygon']:return g
    return unary_union([x for x in g.geoms if x.geom_type in ['Polygon','MultiPolygon']])
def distance(a,b): return math.hypot(a[0]-b[0],a[1]-b[1])

class Router:
    """Visibility graph shortest path inside polygon with holes. All auxiliary vertices explicit."""
    def __init__(self,domain):
        self.domain=domain; self.test=prep(domain.buffer(1e-7)); self.cache={}
        verts=[]
        for poly in polys(domain):
            verts += list(poly.exterior.coords)[:-1]
            for ring in poly.interiors:verts += list(ring.coords)[:-1]
        self.verts=list(dict.fromkeys(tuple(v) for v in verts))
        self.edges=[[] for _ in self.verts]
        for i,a in enumerate(self.verts):
            for j in range(i):
                b=self.verts[j]
                if self.visible(a,b):
                    d=distance(a,b);self.edges[i].append((j,d));self.edges[j].append((i,d))
    def visible(self,a,b):return distance(a,b)<1e-10 or self.test.covers(LineString([a,b]))
    def route(self,a,b):
        a,b=tuple(a),tuple(b)
        if a==b:return [a,b]
        k=(a,b)
        if k in self.cache:return self.cache[k]
        if self.visible(a,b):path=[a,b]
        else:
            verts=self.verts+[a,b]; n=len(self.verts); edges=[e[:] for e in self.edges]+[[],[]]
            for idx,q in [(n,a),(n+1,b)]:
                for j,v in enumerate(self.verts):
                    if self.visible(q,v):
                        d=distance(q,v);edges[idx].append((j,d));edges[j].append((idx,d))
            ds=[float('inf')]*len(verts);prev=[None]*len(verts);ds[n]=0.;todo=[(0.,n)]
            while todo:
                d,i=heapq.heappop(todo)
                if d!=ds[i]:continue
                if i==n+1:break
                for j,w in edges[i]:
                    if d+w < ds[j]-1e-10:ds[j]=d+w;prev[j]=i;heapq.heappush(todo,(ds[j],j))
            if not math.isfinite(ds[n+1]):raise ValueError('No legal route: '+str(k))
            ids=[];v=n+1
            while v is not None:ids.append(v);v=prev[v]
            path=[verts[i] for i in ids[::-1]]
        self.cache[k]=path;self.cache[(b,a)]=path[::-1]
        return path
    def dist(self,a,b):
        r=self.route(a,b);return sum(distance(x,y) for x,y in zip(r,r[1:]))

def coverage(points,r,target,p):
    disks=[Point(q).buffer(r,quad_segs=QUAD) for q in points]
    union=unary_union(disks).intersection(target) if disks else Polygon()
    summed=sum(d.intersection(target).area for d in disks)
    uncovered=polyonly(target.difference(union)); a=target.area; c=union.area; redundant=max(0.,summed-c)
    return dict(effective_area_m2=a,covered_area_m2=c,uncovered_area_m2=uncovered.area,
        coverage_ratio=min(1.,c/a) if a else 1., independent_scan_count=len(points),
        redundant_area_m2=redundant,redundancy_ratio=redundant/summed if summed else 0.,
        uncovered_geometry_wgs84=geo(uncovered,p)),union

def split_at_area(region,target,axis,fraction):
    bounds=region.bounds;lo,hi=bounds[axis],bounds[axis+2]
    for _ in range(60):
        v=(lo+hi)/2
        clip=box(-10000,-10000,v,10000) if axis==0 else box(-10000,-10000,10000,v)
        if target.intersection(clip).area < target.area*fraction:lo=v
        else:hi=v
    v=(lo+hi)/2
    clip=box(-10000,-10000,v,10000) if axis==0 else box(-10000,-10000,10000,v)
    return polyonly(region.intersection(clip)),polyonly(region.difference(clip))

def final_partition(boundary,eff,count,variant):
    target=eff if variant=='effective_area_balanced' else boundary
    if count==3:
        south,north=split_at_area(boundary,target,0,2/3)
        east,west=split_at_area(south,target.intersection(south),1,.5)
        return [north,east,west]
    south,rest=split_at_area(boundary,target,0,1/3)
    middle,north=split_at_area(rest,target.intersection(rest),0,.5)
    out=[]
    for band in [north,middle,south]:out.extend(split_at_area(band,target.intersection(band),1,.5))
    return out

def grid_points(domain,step):
    a,b,c,d=domain.bounds; pr=prep(domain)
    return np.array([(x,y) for x in np.arange(math.floor(a/step)*step+step/2,c,step)
        for y in np.arange(math.floor(b/step)*step+step/2,d,step) if pr.covers(Point(x,y))],dtype=float).reshape(-1,2)

def candidate_layer(region,eff,flight,strict,r):
    legal=region.intersection(eff).intersection(flight)
    if not strict.is_empty:legal=legal.difference(strict.buffer(r+PARAM['disk_clearance_extra_m'],quad_segs=QUAD))
    if legal.is_empty:raise ValueError('No legal scan centers')
    pts=grid_points(legal,PARAM['candidate_grid_step_m']).tolist()
    # Dense legal-boundary candidates improve long narrow regions and edge coverage.
    for po in polys(legal):
        for ring in [po.exterior]+list(po.interiors):
            for d in np.arange(0,ring.length,15.):
                q=ring.interpolate(d); v=po.representative_point(); q=Point(q.x+(v.x-q.x)*1e-7,q.y+(v.y-q.y)*1e-7)
                if legal.covers(q):pts.append([q.x,q.y])
        q=po.representative_point();pts.append([q.x,q.y])
    pts=np.array(sorted(set(tuple(round(c,8) for c in q) for q in pts)),dtype=float)
    # Final rounded candidate must remain legal; buffer tolerance only 0.1 micrometre.
    test=prep(legal.buffer(1e-7));pts=np.array([q for q in pts if test.covers(Point(q))])
    target=region.intersection(eff);samples=grid_points(target,PARAM['target_sample_step_m'])
    masks=np.sum((pts[:,None,:]-samples[None,:,:])**2,axis=2)<=r*r
    return dict(pts=pts,masks=masks,samples=samples,radius=r,selected=[],target=target,legal=legal)

class MissionSolver:
    """Scan arrays use direct legal segments; auxiliary vertices only entry/return."""
    def __init__(self,site,region,uav_id,mode,layers,anchors,budget,return_anchors=None):
        self.site=site;self.region=region;self.uid=uav_id;self.mode=mode;self.layers=layers;self.cp=site['params']
        self.router=Router(polyonly(region.intersection(site['flight'])))
        self.global_router=site['router'];self.anchors=anchors;self.gate=anchors[-1];self.budget=budget
        self.return_anchors=return_anchors if return_anchors is not None else anchors[::-1]
        self.return_gate=self.return_anchors[0];self.direct_cache={}
        self.fixed_entry=sum(self.global_router.dist(a,b) for a,b in zip(anchors,anchors[1:]))
        self.fixed_return=sum(self.global_router.dist(a,b) for a,b in zip(self.return_anchors,self.return_anchors[1:]))
        self.fixed=self.fixed_entry+self.fixed_return
        self.data=[candidate_layer(region,site['eff'],site['flight'],site['strict'],l['r']) for l in layers]
    def qs(self,seqs):return [[tuple(d['pts'][j]) for j in seq] for seq,d in zip(seqs,self.data)]
    def direct(self,a,b):
        k=(tuple(a),tuple(b))
        if k not in self.direct_cache:
            v=distance(a,b) if self.router.visible(a,b) else float('inf')
            self.direct_cache[k]=v;self.direct_cache[k[::-1]]=v
        return self.direct_cache[k]
    def return_choice(self,qs):
        if self.mode=='fixed_layer_forward_reverse':return (self.global_router.dist(qs[0][0],self.return_gate),0)
        last=qs[1][-1];choices=[]
        for i,q in enumerate(qs[0]):
            # Normal return connection is itself a legal direct line, so no
            # hidden non-scanning turn waypoint is needed by B here either.
            if self.global_router.visible(last,q):choices.append((distance(last,q)+self.global_router.dist(q,self.return_gate),i))
        return min(choices) if choices else (float('inf'),0)
    def length(self,seqs):
        qs=self.qs(seqs)
        if any(not q for q in qs):return float('inf')
        inn=[sum(self.direct(a,b) for a,b in zip(q,q[1:])) for q in qs]
        if not all(math.isfinite(x) for x in inn):return float('inf')
        ret,_=self.return_choice(qs)
        if self.mode=='fixed_layer_forward_reverse':return self.fixed+self.global_router.dist(self.gate,qs[0][0])+ret+2*inn[0]
        return self.fixed+self.global_router.dist(self.gate,qs[0][0])+sum(inn)+self.direct(qs[0][-1],qs[1][0])+ret
    def duration(self,seqs):
        cp=self.cp;passes=2 if self.mode=='fixed_layer_forward_reverse' else 1
        vertical=0 if passes==2 else (self.layers[0]['h']-self.layers[1]['h'])*(1/cp['interlayer_descent_mps']+1/cp['return_climb_mps'])
        return self.layers[0]['h']/cp['takeoff_vertical_speed_mps']+self.length(seqs)/cp['horizontal_speed_mps']+sum(map(len,seqs))*cp['scan_seconds']*passes+vertical
    def optimize(self,seqs):
        seqs=[s[:] for s in seqs];best=self.length(seqs)
        for sweep in range(8):
            changed=False
            for li,s in enumerate(seqs):
                for i in range(len(s)):
                    for j in range(i+1,len(s)):
                        trial=[x[:] for x in seqs];trial[li][i:j+1]=trial[li][i:j+1][::-1]
                        v=self.length(trial)
                        if v<best-1e-7:seqs=trial;s=seqs[li];best=v;changed=True
            if not changed:break
        return seqs
    def join_optimize(self,seqs):
        if len(seqs)!=2:return seqs
        qs=self.qs(seqs)
        def objective(s):
            q=self.qs(s);return self.length(s)+max(0.,self.direct(q[0][-1],q[1][0])-20.)*3
        best=objective(seqs);out=seqs
        pairs=sorted((self.direct(a,b),i,j) for i,a in enumerate(qs[0]) for j,b in enumerate(qs[1]))[:8]
        for dist,i,j in pairs:
            if not math.isfinite(dist):continue
            trial=[]
            for li,idx in [(0,i),(1,j)]:
                fixed=seqs[li][idx];remaining=[z for z in seqs[li] if z!=fixed];order=[fixed]
                while remaining:
                    q=self.data[li]['pts'][order[-1]]
                    nxt=min(remaining,key=lambda n:(self.direct(q,self.data[li]['pts'][n]),n));order.append(nxt);remaining.remove(nxt)
                if li==0:order=order[::-1]
                trial.append(order)
            val=self.length(trial)
            for _s in range(5):
                change=False
                for li,s in enumerate(trial):
                    start=0 if li==0 else 1;end=len(s)-1 if li==0 else len(s)
                    for a in range(start,end):
                        for b in range(a+1,end):
                            cand=[x[:] for x in trial];cand[li][a:b+1]=cand[li][a:b+1][::-1]
                            vv=self.length(cand)
                            if vv<val-1e-7:trial=cand;val=vv;change=True
                if not change:break
            obj=objective(trial)
            if self.duration(trial)<=self.budget+1e-8 and obj<best-1e-7:out=trial;best=obj
        return out
    def initial(self):
        orders=[np.argsort(-d['masks'].sum(axis=1),kind='stable') for d in self.data]
        if len(orders)==1:return [[int(orders[0][0])]]
        best=None
        for a in orders[0][:24]:
            for b in orders[1][:24]:
                seqs=[[int(a)],[int(b)]];t=self.duration(seqs)
                if not math.isfinite(t):continue
                gain=sum(float(d['masks'][idx].mean()) for d,idx in zip(self.data,[a,b]))
                key=(gain/t**PARAM['marginal_time_exponent'],-t,-int(a),-int(b))
                if best is None or key>best[0]:best=(key,seqs)
        if best is None:raise ValueError('No initial directly connected pair in candidate shortlist')
        return best[1]
    def solve(self,initial=None):
        seqs=self.optimize(initial if initial is not None else self.initial())
        for iteration in range(40):
            current=self.duration(seqs);options=[]
            for li,d in enumerate(self.data):
                covered=np.any(d['masks'][seqs[li]],axis=0)
                gains=np.logical_and(d['masks'],~covered).sum(axis=1);gains[seqs[li]]=0
                shortlist=np.argsort(-gains,kind='stable')[:PARAM['greedy_shortlist_per_layer']]
                for idx in shortlist:
                    gain=int(gains[idx])
                    if gain<max(2,len(covered)*PARAM['minimum_sample_gain_ratio']):continue
                    bestseq=None;bestt=float('inf')
                    for pos in range(len(seqs[li])+1):
                        trial=[s[:] for s in seqs];trial[li].insert(pos,int(idx));tt=self.duration(trial)
                        if tt<bestt:bestt=tt;bestseq=trial
                    if bestt>self.budget+1e-8:continue
                    balance=1. if len(self.layers)==1 else 1./(PARAM['layer_balance_offset']+float(covered.mean()))
                    score=balance*(gain/len(covered))/max(1,bestt-current)**PARAM['marginal_time_exponent']
                    options.append((score,-bestt,-li,-int(idx),bestseq))
            if not options:break
            seqs=self.optimize(max(options,key=lambda z:z[:4])[-1])
        seqs=self.join_optimize(seqs)
        if self.duration(seqs)>self.budget+1e-6:raise ValueError('Budget failure')
        self.seqs=seqs
        self.diagnostics=dict(uav_id=self.uid,candidate_counts=[len(d['pts']) for d in self.data],sample_counts=[len(d['samples']) for d in self.data],selected_counts=[len(s) for s in seqs],estimated_time_s=self.duration(seqs))
        return self.qs(seqs)


def prune_redundant(solvers):
    """Sequential exact Boolean test, then legal reorder and full timing check."""
    before=[dict(uav_id=s.uid,counts=list(map(len,s.seqs)),time_s=s.duration(s.seqs)) for s in solvers]
    removed=[]
    while True:
        changed=False
        for solver in solvers:
            for li,spec in enumerate(solver.layers):
                for pos,idx in enumerate(solver.seqs[li]):
                    if len(solver.seqs[li])<=1:continue
                    q=solver.data[li]['pts'][idx];disk=Point(q).buffer(spec['r'],quad_segs=QUAD)
                    others=[Point(solver.data[li]['pts'][j]).buffer(spec['r'],quad_segs=QUAD) for j in solver.seqs[li] if j!=idx]
                    own_loss=disk.intersection(solver.data[li]['target']).difference(unary_union(others)).area
                    if own_loss>=.01:continue
                    allothers=[]
                    for other in solvers:
                        for oi,ol in enumerate(other.layers):
                            if ol['id']!=spec['id']:continue
                            for j in other.seqs[oi]:
                                if other is solver and oi==li and j==idx:continue
                                allothers.append(Point(other.data[oi]['pts'][j]).buffer(ol['r'],quad_segs=QUAD))
                    field_loss=disk.intersection(solver.site['eff']).difference(unary_union(allothers)).area
                    if field_loss>=.01:continue
                    trial=[v[:] for v in solver.seqs];trial[li].pop(pos);trial=solver.optimize(trial);trial=solver.join_optimize(trial)
                    after=solver.duration(trial);oldtime=solver.duration(solver.seqs)
                    if math.isfinite(after) and after<=solver.budget and after<oldtime:
                        removed.append(dict(uav_id=solver.uid,layer_id=spec['id'],candidate_index=int(idx),coordinate_m=list(q),own_exclusive_area_m2=own_loss,field_exclusive_area_m2=field_loss,before_time_s=oldtime,after_time_s=after,scan_time_saved_s=solver.cp['scan_seconds']*(2 if len(solver.layers)==1 else 1)))
                        solver.seqs=trial;changed=True;break
                if changed:break
            if changed:break
        if not changed:break
    audit=[]
    for solver in solvers:
        for li,spec in enumerate(solver.layers):
            for pos,idx in enumerate(solver.seqs[li]):
                q=solver.data[li]['pts'][idx];disk=Point(q).buffer(spec['r'],quad_segs=QUAD)
                own=unary_union([Point(solver.data[li]['pts'][j]).buffer(spec['r'],quad_segs=QUAD) for j in solver.seqs[li] if j!=idx])
                others=unary_union([Point(other.data[oi]['pts'][j]).buffer(ol['r'],quad_segs=QUAD) for other in solvers for oi,ol in enumerate(other.layers) if ol['id']==spec['id'] for j in other.seqs[oi] if not (other is solver and oi==li and j==idx)])
                a=disk.intersection(solver.data[li]['target']).difference(own).area;b=disk.intersection(solver.site['eff']).difference(others).area
                row=dict(uav_id=solver.uid,layer_id=spec['id'],waypoint_index=pos+1,waypoint_id='UAV%d_%s_%03d'%(solver.uid,spec['id'],pos+1),own_exclusive_area_m2=a,field_exclusive_area_m2=b,zero_contribution_both=a<.01 and b<.01)
                if row['zero_contribution_both']:
                    trial=[v[:] for v in solver.seqs];trial[li].pop(pos)
                    if trial[li]:trial=solver.join_optimize(solver.optimize(trial))
                    t=solver.duration(trial) if trial[li] else float('inf')
                    row.update(deletion_trial_legal=math.isfinite(t),deletion_trial_time_s=t if math.isfinite(t) else None,retention_reason='Removing this scan point leaves no legal direct scan-array ordering found by deterministic 2-opt/endpoint search; it is retained as an actual paid scan/turn point, not hidden auxiliary.' if not math.isfinite(t) else 'Required nonempty layer or deletion does not improve total time within budget.')
                audit.append(row)
    return dict(method='Sequential deletion, reordering and recomputation after each accepted removal; both own-layer and field-layer exclusive areas <0.01m2; finite legal direct arrays and budget required.',before=before,after=[dict(uav_id=s.uid,counts=list(map(len,s.seqs)),time_s=s.duration(s.seqs)) for s in solvers],removed=removed,final_points=audit)

def make_site(inp,is_final,exconfig):
    f=inp['final_field' if is_final else 'xuchang_field'];p=f['projection'];boundary=Polygon(f['boundary_m'])
    exclusions=[];strict=Polygon();nofly=Polygon();exempt=Polygon()
    if is_final:
        exempt=local(f['terrain']['coverage_exempt_geometry_wgs84'],p)
        exclusions.append(dict(id='historical_terrain_exempt',kind='coverage_exempt',geometry_wgs84=f['terrain']['coverage_exempt_geometry_wgs84'],geometry_status='provisional',source='ESA WorldCover 2021 v200; 10m classes; forest inset 5m / water inset 10m already applied; do not inset again.',source_date='2021',clearance_m=0,scan_disk_intersection_forbidden=False,flight_prohibited=False))
        ss=[]
        for e in exconfig['exclusions']:
            g=local(e['geometry_wgs84'],p);ss.append(g)
            exclusions.append(dict(id=e['id'],kind='scan_disk_forbidden',geometry_wgs84=e['geometry_wgs84'],geometry_status='provisional',source=e['source'],source_date=e.get('source_date','2017-07-24'),clearance_m=0,scan_disk_intersection_forbidden=True,flight_prohibited=False))
        strict=unary_union(ss)
    else:
        nofly=local(f['no_fly_geometry_wgs84'],p)
        exclusions.append(dict(id='xuchang_six_vertex_no_fly',kind='no_fly',geometry_wgs84=f['no_fly_geometry_wgs84'],geometry_status='confirmed',source='02 input fixed six-vertex no-fly polygon; scan disk overlap not prohibited, flight centerline clearance >=5m.',source_date=None,clearance_m=5,scan_disk_intersection_forbidden=False,flight_prohibited=True))
    eff=polyonly(boundary.difference(unary_union([exempt,strict,nofly])))
    # 5.06m round inflation with 8 edges per quadrant has minimum chord
    # radius 5.06*cos(pi/32)>5.03m; avoids overblocking legacy corner gates.
    flight=boundary.buffer(-5.03,join_style=2).difference(nofly.buffer(5.06,quad_segs=8))
    home=f['reference_home'];h=dict(latitude=home['latitude'],longitude=home['longitude'],home_nw_m=nw(home['latitude'],home['longitude'],p),source='nominal_reference_home',is_nominal=True)
    return dict(params=inp['common_parameters'],p=p,boundary=boundary,eff=eff,strict=strict,nofly=nofly,exempt=exempt,flight=flight,router=Router(flight),home=h,exclusions=exclusions,field=f)

def build_uav(solver,points):
    p=solver.site['p'];cp=solver.cp;uid=solver.uid;mode=solver.mode;region=solver.region;passes=2 if mode=='fixed_layer_forward_reverse' else 1
    speed=cp['horizontal_speed_mps'];scan_seconds=cp['scan_seconds'];layers=[]
    for spec,qs in zip(solver.layers,points):
        lid=spec['id'];ids=['UAV%d_%s_%03d'%(uid,lid,i+1) for i in range(len(qs))]
        cov,_=coverage(qs,spec['r'],region.intersection(solver.site['eff']),p);latlon=[ll(q,p) for q in qs]
        layers.append(dict(layer_id=lid,altitude_m=spec['h'],reconnaissance_radius_m=spec['r'],speed_mps=speed,hover_scan_seconds=scan_seconds,scan_passes=passes,waypoint_ids=ids,waypoints_m=qs,waypoints_wgs84=latlon,waypoints_lon_lat_alt=[[v[1],v[0],spec['h']] for v in latlon],execution_order=ids+ids[::-1] if passes==2 else ids[:],coverage=cov))
    segments=[];clock=0.
    def segment(phase,lid,a,b,duration,velocity,waypoint=None,pass_index=None):
        nonlocal clock
        segments.append(dict(segment_id='UAV%d_S%04d'%(uid,len(segments)+1),phase=phase,layer_id=lid,waypoint_id=waypoint,pass_index=pass_index,from_nwz_m=list(a),to_nwz_m=list(b),start_s=clock,end_s=clock+duration,duration_s=duration,horizontal_distance_m=distance(a,b),vertical_distance_m=abs(a[2]-b[2]),speed_mps=velocity));clock+=duration
    def horizontal(phase,lid,path,h):
        for a,b in zip(path,path[1:]):
            if distance(a,b)>1e-9:segment(phase,lid,list(a)+[h],list(b)+[h],distance(a,b)/speed,speed)
    h=layers[0]['altitude_m'];home=solver.site['home']['home_nw_m'];takeoff=cp['takeoff_vertical_speed_mps']
    segment('takeoff',layers[0]['layer_id'],home+[0],home+[h],h/takeoff,takeoff)
    for a,b in zip(solver.anchors,solver.anchors[1:]):horizontal('entry',layers[0]['layer_id'],solver.global_router.route(a,b),h)
    horizontal('entry',layers[0]['layer_id'],solver.global_router.route(solver.gate,points[0][0]),h)
    transition=None
    for li,layer in enumerate(layers):
        lid=layer['layer_id'];h=layer['altitude_m'];qs=points[li];indices=list(range(len(qs)));visits=[(i,1) for i in indices]
        if passes==2:visits += [(i,2) for i in indices[::-1]]
        if li==1:
            a=list(points[0][-1]);b=list(qs[0]);hu=layers[0]['altitude_m'];dh=hu-h;descent=cp['interlayer_descent_mps']
            start=clock;segment('layer_transition',None,a+[hu],a+[h],dh/descent,descent)
            assert solver.router.visible(a,b),'Interlayer actual direct array link illegal'
            horizontal('layer_transition',None,[a,b],h);d=distance(a,b)
            transition=dict(from_waypoint_id=layers[0]['waypoint_ids'][-1],to_waypoint_id=layer['waypoint_ids'][0],horizontal_distance_m=d,vertical_distance_m=dh,descent_speed_mps=descent,descent_speed_status='accepted_provisional',duration_s=clock-start,preferred_horizontal_limit_m=20,preferred_limit_met=d<=20,note='原位下降，再直接水平至下层首点；直连全段已核验，无隐藏辅助点；下降速度暂定。'+('满足20m软目标。' if d<=20 else '超过20m软目标：优先保留独立双层覆盖及直连合法性与预算。'))
        prev=None
        for idx,passidx in visits:
            if prev is not None:
                assert solver.router.visible(qs[prev],qs[idx]),'Actual scan array direct link illegal'
                horizontal('scan_transit',lid,[qs[prev],qs[idx]],h)
            q=list(qs[idx])+[h];segment('scan',lid,q,q,scan_seconds,None,layer['waypoint_ids'][idx],passidx);prev=idx
    last=list(points[-1][0] if passes==2 else points[-1][-1]);h=layers[-1]['altitude_m'];lid=layers[-1]['layer_id'];climb_s=0.;climb_speed=None
    if passes==1:
        hu=layers[0]['altitude_m'];climb_speed=cp['return_climb_mps'];climb_s=(hu-h)/climb_speed
        segment('return',None,last+[h],last+[hu],climb_s,climb_speed);h=hu;lid='upper'
    _,chosen=solver.return_choice(points);join=list(points[0][chosen]);connection=[last,join]
    horizontal('return',lid,connection,h)
    path=[join]
    for a,b in [(join,solver.return_gate)]+list(zip(solver.return_anchors,solver.return_anchors[1:])):
        route=solver.global_router.route(a,b)
        for q in route[1:]:
            if distance(path[-1],q)>1e-9:path.append(list(q))
    horizontal('return',lid,path,h)
    solver.return_selection=dict(source_uav_id=uid,waypoint_index=chosen+1,waypoint_id=layers[0]['waypoint_ids'][chosen],connection_path_m=connection,return_path_m=path,return_climb_mps=climb_speed,return_climb_s=climb_s,return_connection_distance_m=distance(last,join),return_route_distance_m=sum(distance(a,b) for a,b in zip(path,path[1:])))
    timing={phase+'_s':sum(s['duration_s'] for s in segments if s['phase']==phase) for phase in ['takeoff','entry','scan_transit','scan','layer_transition','return']}
    limit=cp['mission_limit_s'];reserve=cp['preferred_reserve_s']
    timing.update(mission_time_s=clock,mission_limit_s=limit,planning_reserve_s=reserve,budget_with_reserve_s=clock+reserve,remaining_s=limit-clock,within_limit=clock<=limit,within_planning_target=clock<=limit-reserve)
    assert abs(clock-solver.duration(solver.seqs))<1e-6,'Time model mismatch'
    geom=geo(region,p);simple=region.geom_type=='Polygon' and not region.interiors
    proposal=dict(proposed_only=True,ros_topics=None,upper=None,lower=None)
    for l in layers:proposal[l['layer_id']]=dict(waypoint_ids=l['waypoint_ids'],waypoints_lon_lat_alt=l['waypoints_lon_lat_alt'],region_geometry_wgs84=geom,scan_seconds=scan_seconds,scan_passes=passes,execution_order=l['execution_order'])
    return dict(uav_id=uid,subregion_id='region_%d'%uid,partition_group=layers[0]['layer_id'] if passes==2 else 'all',region_geometry_wgs84=geom,polygon_m=list(region.exterior.coords)[:-1] if simple else None,reference_home=solver.site['home'],layer_order=[l['layer_id'] for l in layers],layers=layers,segments=segments,timing=timing,layer_transition=transition,external_program_b_proposal=proposal,compatibility_notes=['离线主schema保持v1、ros_topics=null；已知实际话题及数组映射见topic_mapping.json。尚未导入或部署。','扫描数组及换层全部相邻点直接连接合法，不能把非扫描辅助点偷偷放进扫描数组。','A唯一物理层(含lower)映射path_stage_1，path_stage_2为空，按旧单层接口正逆两趟；不得给task.layers单层数组。' if passes==2 else '双层按upper/lower映射path_stage_1/2各一趟；下层结束先回升再接入上层目录；返航回升2m/s为规划暂估、非用户确认。']+([] if simple else ['本区域含洞或多面，polygon_m=null；必须使用完整GeoJSON。']))

def build_scene(inp,spec,site,budget,variant):
    is_final=spec['site']=='final_field';mode=spec['execution_mode'];count=3 if mode=='fixed_layer_forward_reverse' else 6
    regions=final_partition(site['boundary'],site['eff'],count,variant) if is_final else [Polygon(u['polygon_m']) for u in site['field']['baseline_uavs']]
    if count==3:regions=regions+regions
    uavs=[];diagnostics=[];solvers=[];cp=site['params']
    for uid,region in enumerate(regions,1):
        if count==3:
            lid='upper' if uid<=3 else 'lower';s=spec[lid+'_uavs'][str(uid)];layers=[dict(id=lid,h=s['altitude_m'],r=s['radius_m'])]
        else:
            s=spec['heights_by_uav'][str(uid)];layers=[dict(id=lid,h=s[lid+'_height_m'],r=cp[lid+'_scan_radius_m']) for lid in ['upper','lower']]
        anchors=[site['home']['home_nw_m']]
        if not is_final:anchors += site['field']['baseline_uavs'][uid-1]['old_entry_path_m'][1:-1]
        return_anchors=[site['home']['home_nw_m']] if is_final else site['field']['baseline_uavs'][uid-1]['old_return_path_from_last_waypoint_m'][1:]
        solver=MissionSolver(site,region,uid,mode,layers,anchors,budget,return_anchors);pts=solver.solve()
        uav=build_uav(solver,pts);uavs.append(uav);diagnostics.append(solver.diagnostics);solvers.append(solver)
        print('%s %s budget=%g UAV%d points=%s time=%.2f coverage=%s'%(spec['scene_id'],variant,budget,uid,[len(q) for q in pts],uav['timing']['mission_time_s'],[round(l['coverage']['coverage_ratio'],4) for l in uav['layers']]),flush=True)
    pruning=prune_redundant(solvers)
    pruning['refill_rounds']=[]
    for refinement in range(PARAM['post_pruning_refill_rounds']):
        old_seqs=[[x[:] for x in sol.seqs] for sol in solvers]
        for sol in solvers:sol.solve(initial=sol.seqs)
        added=sum(sum(map(len,sol.seqs))-sum(map(len,before)) for sol,before in zip(solvers,old_seqs))
        if added==0:break
        audit=prune_redundant(solvers)
        pruning['refill_rounds'].append(dict(round=refinement+1,added_points=added,before_pruning=audit['before'],after_pruning=audit['after'],deleted_points=len(audit['removed'])))
        pruning['removed'].extend(audit['removed']);pruning['after']=audit['after'];pruning['final_points']=audit['final_points']
    uavs=[build_uav(s,s.qs(s.seqs)) for s in solvers]
    for d,sol in zip(diagnostics,solvers):d.update(selected_counts=[len(x) for x in sol.seqs],estimated_time_s=sol.duration(sol.seqs))
    selections={str(s.uid):s.return_selection for s in solvers}
    p=site['p'];metrics={};unions={}
    for lid in ['upper','lower']:
        pts=[q for u in uavs for l in u['layers'] if l['layer_id']==lid for q in l['waypoints_m']]
        metrics[lid],unions[lid]=coverage(pts,cp[lid+'_scan_radius_m'],site['eff'],p)
    checks=[]
    for group in ['upper','lower'] if count==3 else ['all']:
        regs=[r for r,u in zip(regions,uavs) if u['partition_group']==group];union=unary_union(regs);partition=site['boundary'].difference(site['nofly'])
        checks.append(dict(group=group,overlap_area_m2=max(0.,sum(r.area for r in regs)-union.area),gap_area_m2=partition.difference(union).area,outside_area_m2=union.difference(partition).area,all_regions_valid=all(r.is_valid for r in regs)))
    metrics.update(both_layers_coverage_ratio=unions['upper'].intersection(unions['lower']).area/site['eff'].area,either_layer_coverage_ratio=unions['upper'].union(unions['lower']).area/site['eff'].area,maximum_completion_time_s=max(u['timing']['mission_time_s'] for u in uavs),total_aircraft_time_s=sum(u['timing']['mission_time_s'] for u in uavs),all_aircraft_within_limit=all(u['timing']['within_limit'] for u in uavs),partition_checks=checks)
    assumptions=[dict(id='nominal_homes',description='六机共用输入固定参考起点；未提供各机本次开机实测GPS。',status='unconfirmed',impact='运行前必须接入各机真实起降GPS，重新核验全部距离、时间及净距。'),dict(id='altitudes_and_descent',description='双层高度47/50/53m与34/37/40m、下降2m/s，用户接受暂定，待实机核对；高度相对各机起点。',status='accepted_provisional',impact='名义估时；未证明实飞可行。'),dict(id='budget',description='水平5m/s，扫描50s/次，起飞6m/s；预算1380s，规划余量30s仅加一次。返场到降落点上方。',status='confirmed',impact='未计风、加减速、目标跟踪、额外等待；30秒为未建模因素预留。'),dict(id='scope',description='不修改、不连接、不部署竞赛程序或B，不生成真实mission_id或assignment_checksum。',status='confirmed',impact='本包是独立离线交接，需后续接口适配。')]
    if is_final:
        assumptions += [dict(id='strict_exclusion_draft',description='东南强禁扫已整体替换为用户8点折线+原8/9/10边推导的Q南/Q东连接；补点非用户实测。池塘仍使用2017影像草案。',status='unconfirmed',impact='完整扫描圆不得侵入候选区；真实边界确认前场景必须draft。'),dict(id='historical_terrain',description='使用输入已内缩的ESA WorldCover2021豁免，不再内缩；约10m分类，非现状测绘。',status='accepted_provisional',impact='免扫区允许圆盘触及及飞越；不得在豁免区设置扫描中心。'),dict(id='legacy_scope',description='仅前10点原序边界；不继承旧编号禁扫、building首点、UAV3/5特殊分区或补扫；取消旧专用进返场及落点避让约束。',status='confirmed',impact='仍保守保留外边界5m中心线净距，飞行线相交无动态避碰保证。')]
    else:assumptions += [dict(id='xuchang_inheritance',description='保持输入当前六区机号与全部边界，不再执行旧交换。分别保留旧进场和旧返场路径的中间通道/门点及辅助点；仅替换首末扫描点，连接绕障重新计算。',status='confirmed',impact='不继承旧75点、10s或45m；UAV5大区允许覆盖缺口。'),dict(id='no_fly',description='六点禁飞区内不侦察、不飞行；中心和全部航段边界净距5m，扫描圆盘跨入不作为强禁扫。',status='confirmed',impact='单机内部路径也绕障，辅助点不扫描。')]
    assumptions.append(dict(id='return_climb',description='双层返航先在下层末点原位升到本机上层，再直连接入选定上层航点的返航路线。return_climb_mps='+str(cp['return_climb_mps'])+'仅为本轮规划暂估，非用户确认或实测。',status='unconfirmed',impact='回升和接入均计入return_s；另交1/2/3m/s敏感性。A固定层不回升。'))
    if is_final:assumptions.append(dict(id='local_terrain_audit',description='旧东南范围内新树林外约1836.74平方米历史豁免的归属未确认。主方案保留旧地类，另交仅局部过时森林修正敏感性及完整几何，未删除其他森林或水体。',status='unconfirmed',impact='详见terrain_mask_audit.json；修改mask后须复算点位和强禁扫。'))
    pending=['返航回升2m/s尚非用户确认，需实机核对','每机本次开机实测起降GPS与相对高度基准','暂定高度、下降2m/s和未建模实飞耗时','时空防撞、通信/控制器动态、风与目标跟踪未验证']
    if is_final:pending += ['池塘草案边界及新树林Q南/Q东推导连接待确认','最新森林/水体边界测绘；2017影像与2021分类不证明现状','是否允许进返场越出外边界尚待确认；本方案保守保持内部']
    scene=dict(scene_id=spec['scene_id'],display_name=spec['display_name'],status='draft',execution_mode=mode,coordinate_frame='WGS84',local_frame='LOCAL_NORTH_WEST',projection=p,boundary_geometry_wgs84=geo(site['boundary'],p),reference_home=site['home'],exclusions=site['exclusions'],effective_area_geometry_wgs84=geo(site['eff'],p),assumptions=assumptions,pending_confirmations=pending,uavs=uavs,scene_metrics=metrics,validation_report_key=spec['scene_id'])
    return scene,dict(return_selections=selections,redundancy_audit=pruning,scene_id=spec['scene_id'],variant=variant,budget_s=budget,uavs=diagnostics,upper_coverage=metrics['upper']['coverage_ratio'],lower_coverage=metrics['lower']['coverage_ratio'],maximum_completion_time_s=metrics['maximum_completion_time_s'],total_aircraft_time_s=metrics['total_aircraft_time_s'])

def export_aux(result,out):
    with (out/'waypoints.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow('scene_id,uav_id,layer_id,waypoint_id,waypoint_index_1based,latitude,longitude,north_m,west_m,relative_altitude_m,scan_radius_m,scan_seconds,scan_passes'.split(','))
        for s in result['scenes']:
            for u in s['uavs']:
                for l in u['layers']:
                    for i,(wid,q,latlon) in enumerate(zip(l['waypoint_ids'],l['waypoints_m'],l['waypoints_wgs84']),1):w.writerow([s['scene_id'],u['uav_id'],l['layer_id'],wid,i]+list(latlon)+list(q)+[l['altitude_m'],l['reconnaissance_radius_m'],l['hover_scan_seconds'],l['scan_passes']])
    with (out/'scan_visits.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow('scene_id,uav_id,visit_index_1based,layer_id,pass_index_1based,waypoint_id,start_s,end_s'.split(','))
        for s in result['scenes']:
            for u in s['uavs']:
                visits=[v for v in u['segments'] if v['phase']=='scan']
                for i,v in enumerate(visits,1):w.writerow([s['scene_id'],u['uav_id'],i,v['layer_id'],v['pass_index'],v['waypoint_id'],v['start_s'],v['end_s']])
    features=[]
    def add(s,kind,id,geom,u=None,l=None,extra=None):
        if geom is None:return
        props=dict(scene_id=s['scene_id'],uav_id=u,layer_id=l,kind=kind,id=id)
        if extra:props.update(extra)
        features.append(dict(type='Feature',properties=props,geometry=geom))
    for s in result['scenes']:
        add(s,'boundary','boundary',s['boundary_geometry_wgs84'])
        for e in s['exclusions']:add(s,e['kind'],e['id'],e['geometry_wgs84'],extra={'geometry_status':e['geometry_status']})
        for lid in ['upper','lower']:add(s,'uncovered','scene_uncovered_'+lid,s['scene_metrics'][lid]['uncovered_geometry_wgs84'],l=lid)
        for u in s['uavs']:
            uid=u['uav_id'];add(s,'subregion',u['subregion_id'],u['region_geometry_wgs84'],uid,u['partition_group'] if u['partition_group']!='all' else None)
            for l in u['layers']:
                lid=l['layer_id'];add(s,'uncovered','uav_uncovered_'+lid,l['coverage']['uncovered_geometry_wgs84'],uid,lid)
                for wid,q in zip(l['waypoint_ids'],l['waypoints_lon_lat_alt']):add(s,'waypoint',wid,{'type':'Point','coordinates':q},uid,lid,{'scan_radius_m':l['reconnaissance_radius_m'],'scan_passes':l['scan_passes']})
            for seg in u['segments']:
                if seg['phase'] in ['takeoff','scan']:continue
                coords=[]
                for q in [seg['from_nwz_m'],seg['to_nwz_m']]:latlon=ll(q,s['projection']);coords.append([latlon[1],latlon[0],q[2]])
                add(s,seg['phase'],seg['segment_id'],{'type':'LineString','coordinates':coords},uid,seg['layer_id'],{'start_s':seg['start_s'],'end_s':seg['end_s'],'z_reference':'relative_to_each_aircraft_takeoff_home'})
    dump(out/'planning_result.geojson',dict(type='FeatureCollection',name='Offline multilayer plan; Z is relative altitude, NOT WGS84 ellipsoidal altitude',features=features))

def feasibility_bounds(inp,sites):
    cp=inp['common_parameters'];result=dict(calculation_order='Computed before candidate optimization',limitations='Optimistic disk area and scanning/vertical time bounds exclude all horizontal flight, overlaps and spills; necessary conditions only.',scenes=[])
    for spec in inp['scenes']:
        site=sites[spec['site']];area=site['eff'].area;rows=[];fixed=spec['scene_id']=='final_scene_1'
        if fixed:
            for lid in ['upper','lower']:
                r=cp[lid+'_scan_radius_m'];num=sum(math.floor((cp['mission_limit_s']-v['altitude_m']/cp['takeoff_vertical_speed_mps'])/(2*cp['scan_seconds'])) for v in spec[lid+'_uavs'].values());ceiling=num*math.pi*r*r
                rows.append(dict(layer_id=lid,radius_m=r,ideal_max_independent_points=num,ideal_area_ceiling_m2=ceiling,ideal_coverage_ratio_ceiling=min(1,ceiling/area),description='%s独立点粗上限%d，理想面积上界%.6f平方米；未扣水平飞行、外溢和重叠。'%(lid,num,ceiling)))
            regs=final_partition(site['boundary'],site['eff'],3,'effective_area_balanced')
            for uid,reg in enumerate(regs+regs,1):
                lid='upper' if uid<=3 else 'lower';r=cp[lid+'_scan_radius_m'];h=spec[lid+'_uavs'][str(uid)]['altitude_m'];a=reg.intersection(site['eff']).area;n=math.ceil(a/(math.pi*r*r));t=n*2*cp['scan_seconds']+h/cp['takeoff_vertical_speed_mps']
                rows.append(dict(uav_id=uid,layer_id=lid,effective_area_m2=a,minimum_independent_points_for_full_area=n,full_coverage_time_lower_bound_s=t,not_ruled_out_by_time_lower_bound=t<=cp['mission_limit_s']))
        else:
            regs=[Polygon(u['polygon_m']) for u in site['field']['baseline_uavs']] if spec['site']=='xuchang_field' else final_partition(site['boundary'],site['eff'],6,'effective_area_balanced')
            for uid,reg in enumerate(regs,1):
                a=reg.intersection(site['eff']).area;nu=math.ceil(a/(math.pi*cp['upper_scan_radius_m']**2));nl=math.ceil(a/(math.pi*cp['lower_scan_radius_m']**2));h=spec['heights_by_uav'][str(uid)];v=h['upper_height_m']/cp['takeoff_vertical_speed_mps']+(h['upper_height_m']-h['lower_height_m'])*(1/cp['interlayer_descent_mps']+1/cp['return_climb_mps']);t=(nu+nl)*cp['scan_seconds']+v
                rows.append(dict(uav_id=uid,effective_area_m2=a,minimum_upper_points_for_full_area=nu,minimum_lower_points_for_full_area=nl,full_coverage_time_lower_bound_s=t,not_ruled_out_by_time_lower_bound=t<=cp['mission_limit_s'],max_combined_scan_count_ignoring_horizontal_flight=math.floor((cp['mission_limit_s']-v)/cp['scan_seconds'])))
        result['scenes'].append(dict(scene_id=spec['scene_id'],effective_area_m2=area,bounds=rows))
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path,default=ROOT/'inputs/02_规划输入_v2.json');ap.add_argument('--exclusions',type=Path,default=ROOT/'exclusions_draft.json');ap.add_argument('--output-dir',type=Path,default=ROOT);ap.add_argument('--reuse-candidates',action='store_true',help='Re-export already generated candidates without recomputing optimization.');args=ap.parse_args();out=args.output_dir;out.mkdir(parents=True,exist_ok=True)
    inp=json.loads(args.input.read_text(encoding='utf-8'));exc=json.loads(args.exclusions.read_text(encoding='utf-8'))
    PARAM['mission_parameters']=inp['common_parameters']
    config_path=out/'planner_config.json'
    if config_path.exists():config=json.loads(config_path.read_text(encoding='utf-8'))
    else:
        config=dict(generated_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),parameters=PARAM,exclusion_input='exclusions_draft.json',description='Timestamp is persisted for byte-reproducible reruns. Algorithm parameters are explicitly versioned in planner.py; this file records the exact run configuration.')
        dump(config_path,config)
    config['parameters']=PARAM
    dump(config_path,config)
    if args.reuse_candidates:
        result=json.loads((out/'candidates/coverage_priority.json').read_text(encoding='utf-8'));comparison=json.loads((out/'candidate_comparison.json').read_text(encoding='utf-8'))
    else:
        sites={k:make_site(inp,k=='final_field',exc) for k in ['final_field','xuchang_field']}
        bounds=feasibility_bounds(inp,sites);dump(out/'feasibility_bounds.json',bounds)
        print('Computed area ceilings and scan/vertical time lower bounds before optimization.',flush=True)
        comparison=[];all_results={}
        for name,budget in [('coverage_priority',1350.),('more_reserve',1230.)]:
            scenes=[];chosen_selections={};chosen_pruning={}
            for spec in inp['scenes']:
                candidates=[]
                variants=PARAM['partition_candidates'] if spec['site']=='final_field' else ['input_fixed_regions']
                for variant in variants:
                    scene,diag=build_scene(inp,spec,sites[spec['site']],budget,variant);candidates.append((scene,diag));comparison.append(dict(candidate=name,**{k:v for k,v in diag.items() if k not in ['return_selections','redundancy_audit']}))
                scene,diag=max(candidates,key=lambda c:(c[1]['upper_coverage']+c[1]['lower_coverage'],-c[1]['maximum_completion_time_s']))
                scenes.append(scene);chosen_selections[spec['scene_id']]=diag['return_selections'];chosen_pruning[spec['scene_id']]=diag['redundancy_audit']
                for c in comparison:
                    if c['candidate']==name and c['scene_id']==spec['scene_id']:c['selected_partition']=c['variant']==diag['variant']
            result=dict(schema_version=1,contract_type='competition_multilayer_planning_handoff',compatibility='requires_future_ground_onboard_and_3d_adapter',plan_id='offline_20261017_'+name+'_v2',source_inputs_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest(),generated_at=config['generated_at'],planner=dict(name='deterministic_coverage_insertion_visibility',version='2.0.0',random_seed=20261017,parameters=PARAM,dependencies=['Python '+platform.python_version(),'numpy '+np.__version__,'shapely '+shapely.__version__,'jsonschema 4.23.0']),scenes=scenes)
            dump(out/'candidates'/('%s.json'%name),result);dump(out/'candidates'/(name+'_return_selections.json'),dict(scenes=chosen_selections));dump(out/'candidates'/(name+'_redundancy_audit.json'),dict(scenes=chosen_pruning));all_results[name]=result
        dump(out/'candidate_comparison.json',comparison);result=all_results['coverage_priority']
    dump(out/'planning_result.json',result)
    for dest,suffix in [('return_selections.json','_return_selections.json'),('redundancy_audit.json','_redundancy_audit.json')]:dump(out/dest,json.loads((out/'candidates'/('coverage_priority'+suffix)).read_text(encoding='utf-8')))
    # Reload serialized file before deriving exports: one data source for all artifacts.
    result=json.loads((out/'planning_result.json').read_text(encoding='utf-8'));export_aux(result,out)
    print('Wrote',out/'planning_result.json',flush=True)

if __name__=='__main__':main()
