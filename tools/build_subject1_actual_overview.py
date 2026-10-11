"""从固定JSON画扫描圆与航点，便于复核部分覆盖；仅需matplotlib。"""
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Polygon as MplPolygon, PathPatch
from matplotlib.path import Path as MplPath
from matplotlib.lines import Line2D
ROOT=Path(__file__).resolve().parents[1]


def build():
    plan=json.loads((ROOT/'competition_backend/competition_backend/subject1_actual_prepared.json').read_text(encoding='utf-8'))['plan']
    area=plan['search_area'];info=area['coverage']
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    colors=['#185bd1','#16964a','#d26612','#8738c4','#008c9e','#c52354']
    def xy(points):return [(-p[1],p[0]) for p in points]
    def line(ax,points,**kwargs):
        p=xy(points);return ax.plot([q[0] for q in p],[q[1] for q in p],**kwargs)
    def layer(ax,geometry,color):
        polys=[geometry['coordinates']] if geometry['type']=='Polygon' else geometry['coordinates'] if geometry['type']=='MultiPolygon' else []
        for poly in polys:
            vertices=[];codes=[]
            for ring in poly:
                vertices.extend(xy(ring));codes.extend([MplPath.MOVETO]+[MplPath.LINETO]*(len(ring)-2)+[MplPath.CLOSEPOLY])
            ax.add_patch(PathPatch(MplPath(vertices,codes),facecolor=color,edgecolor='none',alpha=.5))
    fig,(left,right)=plt.subplots(1,2,figsize=(16,10),layout='constrained')
    for ax in (left,right):
        boundary=area['points_m'];line(ax,boundary+[boundary[0]],color='#283645',lw=1.5)
        layer(ax,area['terrain_layers_m']['forest'],'#527c4e')
        layer(ax,area['terrain_layers_m']['water'],'#73bdd5')
        ax.set_aspect('equal');ax.set_xlabel('东向 / 米');ax.set_ylabel('北向 / 米');ax.grid(alpha=.16)
        depot=xy([area['departure_point_m']])[0];ax.scatter(*depot,marker='*',s=110,c='black',zorder=10)
    clip=MplPolygon(xy(area['points_m']),closed=True,transform=left.transData)
    for uid,item in plan['planned_uavs'].items():
        task=item['task'];color=colors[int(uid)-1]
        line(left,task['polygon_m']+[task['polygon_m'][0]],color=color,lw=.7,alpha=.6,ls='--')
        for point in task['waypoints_m']:
            disk=Circle(xy([point])[0],task['reconnaissance_radius_m'],facecolor=color,edgecolor=color,alpha=.10,lw=.8)
            disk.set_clip_path(clip);left.add_patch(disk)
        line(left,task['waypoints_m'],color=color,lw=1.8,marker='o',ms=3,label=f'UAV{uid}：{task["scan_count"]}点 / {task["reconnaissance_radius_m"]:g}m')
    uav3=plan['planned_uavs']['3']['task'];p=uav3['polygon_m']
    line(right,p+[p[0]],color='#d26612',ls='--',lw=1)
    for index,point in enumerate(uav3['waypoints_m'],1):
        center=xy([point])[0]
        right.add_patch(Circle(center,uav3['reconnaissance_radius_m'],facecolor='#e69236',edgecolor='#d26612',alpha=.13))
        right.annotate(str(index),center,xytext=(6,5),textcoords='offset points',fontsize=10)
    line(right,uav3['flight_path_m'],color='#638ba8',ls='--',lw=1.2)
    line(right,uav3['waypoints_m'],color='#b84e08',lw=2.5,marker='o',ms=5)
    left.set_title('六机扫描圆及航点 · 不要求补齐全部空白',fontsize=14)
    right.set_title(f'UAV3：{uav3["scan_count"]}点 · 允许放弃低收益林缘\n进返场与扫描航迹均保留5米边界间距',fontsize=14)
    left.legend(loc='lower center',bbox_to_anchor=(.5,-.23),ncol=2,fontsize=10)
    right.legend(handles=[Line2D([0],[0],color='#b84e08',marker='o',label='UAV3扫描顺序'),Line2D([0],[0],color='#638ba8',ls='--',label='完整参考航迹'),Line2D([0],[0],color='#527c4e',lw=7,label='树林'),Line2D([0],[0],color='#73bdd5',lw=7,label='湖泊')],loc='lower center',bbox_to_anchor=(.5,-.23),ncol=2)
    fig.suptitle('科目一比赛使用场景 · 每机最多16点 · 40秒/点\n有效区域覆盖 {:.2%}；{}个航点；覆盖按59/56/53/50/47/44米高度计算'.format(info['coverage_ratio'],info['total_scan_count']),fontsize=17)
    target=ROOT/'docs/subject1_actual_coverage.png'
    fig.savefig(target,dpi=150,facecolor='white');plt.close(fig)
    print(target)


if __name__=='__main__':build()
