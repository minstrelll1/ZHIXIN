"""用固定科目二JSON重绘覆盖和进场分离图；不连接地面端或飞机。"""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon, PathPatch
from matplotlib.path import Path as MplPath

ROOT=Path(__file__).resolve().parents[1]


def build():
    p=json.loads((ROOT/'competition_backend/competition_backend/subject2_actual_prepared.json').read_text(encoding='utf-8'))['plan']
    a=p['search_area']; c=a['coverage']
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    colors=['#185bd1','#16964a','#d26612','#8738c4','#008c9e','#c52354']
    def xy(points):return [(-q[1],q[0]) for q in points]
    def line(ax,points,**kw):
        q=xy(points);ax.plot([r[0] for r in q],[r[1] for r in q],**kw)
    def layer(ax,geometry,color):
        polygons=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates'] if geometry['type']=='MultiPolygon' else []
        for polygon in polygons:
            vertices=[];codes=[]
            for ring in polygon:
                vertices.extend(xy(ring));codes.extend([MplPath.MOVETO]+[MplPath.LINETO]*(len(ring)-2)+[MplPath.CLOSEPOLY])
            ax.add_patch(PathPatch(MplPath(vertices,codes),facecolor=color,edgecolor='none',alpha=.35))
    fig,axes=plt.subplots(1,3,figsize=(19,10),layout='constrained',gridspec_kw={'width_ratios':[1.1,1.1,1]})
    boundary=a['points_m'];depot=xy([a['departure_point_m']])[0]
    for ax in axes:
        ax.set_aspect('equal');ax.grid(alpha=.14);ax.set_xlabel('东 / 米');ax.set_ylabel('北 / 米')
        line(ax,boundary+[boundary[0]],color='#283645',lw=1.2)
        layer(ax,a['terrain_layers_m']['forest'],'#527c4e');layer(ax,a['terrain_layers_m']['water'],'#73bdd5')
        for ring,ids in zip(a['scan_exclusion_polygons_m'],a['scan_exclusion_point_ids']):
            ax.add_patch(Polygon(xy(ring),closed=True,facecolor='#dc2626',edgecolor='#dc2626',alpha=.5,hatch='///'))
        ax.scatter(*depot,marker='*',s=100,c='black',zorder=10)
    for focus in a.get('priority_regions',[]):
        ring=focus['polygon_m']
        line(axes[0],ring+[ring[0]],color='#d26612',lw=.9,ls=':',alpha=.65)
        axes[0].annotate('UAV3原重点覆盖区',xy([ring[0]])[0],xytext=(-20,10),textcoords='offset points',fontsize=9,color='#a04b09')
    if a.get('cooperative_region'):
        ring=a['cooperative_region']['polygon_m']
        line(axes[0],ring+[ring[0]],color='#172c39',lw=1.8,ls='-.')
    clip=Polygon(xy(boundary),closed=True,transform=axes[0].transData)
    for uid,item in p['planned_uavs'].items():
        t=item['task'];color=colors[int(uid)-1];scans=t['waypoints_m']
        line(axes[0],t['polygon_m']+[t['polygon_m'][0]],color=color,lw=1.3,ls='-',alpha=.8)
        for point in scans:
            circle=Circle(xy([point])[0],t['reconnaissance_radius_m'],facecolor=color,edgecolor=color,alpha=.15,lw=.7)
            circle.set_clip_path(clip);axes[0].add_patch(circle)
        line(axes[0],scans,color=color,lw=1.6,marker='o',ms=3,
             label='UAV%s：%d点 × 2趟 / %.0f秒' % (uid,len(scans),t['mission_time_s']))
        for ax in axes[1:]:
            line(ax,t['entry_path_m'],color=color,lw=1.8,label='UAV'+uid)
            ax.scatter(*xy([scans[0]])[0],c=color,s=35,zorder=10)
    for name,b in a['buildings'].items():
        pos=xy([b['point_m']])[0]
        for ax in axes[:2]:
            ax.scatter(*pos,marker='s',s=32,c='black',zorder=12)
            ax.annotate(name+' / UAV'+str(b['uav_id']),pos,xytext=(6,10),textcoords='offset points',fontsize=9)
    allocation=a.get('western_rebalance',{})
    if allocation.get('optional_included'):
        pos=xy([allocation['requested_optional_point_m']])[0]
        axes[0].scatter(*pos,marker='x',s=40,c='#d26612',zorder=15)
        axes[0].annotate('北侧指定位置\n补扫点偏移%.1fm'%allocation['optional_offset_m'],pos,
            xytext=(-100,15),textcoords='offset points',fontsize=9,color='#a04b09')
    first=xy([p['planned_uavs']['3']['task']['waypoints_m'][0]])[0]
    axes[0].annotate('UAV3首点',first,xytext=(-55,-16),textcoords='offset points',fontsize=9,color='#a04b09')
    axes[2].add_patch(Circle(depot,30,fill=False,edgecolor='#555',ls='--',lw=1))
    axes[2].set_xlim(depot[0]-65,depot[0]+230);axes[2].set_ylim(depot[1]-190,depot[1]+85)
    axes[0].set_title('六机子区互不重叠，扫描圆边缘允许交叠\n彩色实线为子区边界；红色斜线为禁扫区',fontsize=12)
    axes[1].set_title('六机飞向第一个扫描点的路线\n首点覆盖building2 / 3 / 4',fontsize=12)
    axes[2].set_title('操场出口放大\n30米扇出区外，首航段间距≥3米',fontsize=12)
    axes[0].legend(loc='upper center',bbox_to_anchor=(.5,-.14),ncol=2,fontsize=9)
    axes[1].legend(loc='upper center',bbox_to_anchor=(.5,-.14),ncol=3,fontsize=9)
    fig.suptitle('科目二比赛使用场景 · 正序一趟 + 逆序一趟 · 每次扫描30秒\n%d个航点 / %d次扫描 · 有效覆盖 %.2f%% · 最长预计 %.1f秒（上限840秒）' %
        (c['total_scan_count'],c['total_scan_visit_count'],c['coverage_ratio']*100,c['maximum_completion_time_s']),fontsize=17)
    target=ROOT/'docs/subject2_actual_coverage.png'
    temporary=target.with_name(target.stem+'.tmp.png')
    fig.savefig(temporary,dpi=150,facecolor='white',bbox_inches='tight');plt.close(fig)
    temporary.replace(target)
    print(target)


if __name__=='__main__':build()
