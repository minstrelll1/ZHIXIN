"""用真实规划函数生成许昌起降点避让示例；不连接飞机、不下发任务。"""
import json
import math
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'competition_backend'))
from competition_backend.transit_routes import attach_routes,rebase_gps_routes_for_takeoff
from competition_backend.xuchang_small_scene import load_xuchang_small_plan


def build_data():
    from shapely.geometry import Polygon,LineString,Point
    from shapely.ops import nearest_points
    plan=attach_routes(load_xuchang_small_plan());a=plan['search_area'];p=a['coverage']['projection']
    # UAV2恰好放在UAV1原进场连接上，清楚展示新增绕行。
    offsets={1:(0.,0.),2:(10.,-45./7),3:(-10.,10.),4:(0.,10.),5:(-10.,0.),6:(10.,10.)}
    homes={str(uid):dict(latitude=p['latitude']+n/p['north_m_per_degree'],
        longitude=p['longitude']-w/p['west_m_per_degree'],boot_id='example-boot-%d'%uid,
        source='first_valid_unarmed_gps_per_boot') for uid,(n,w) in offsets.items()}
    a['takeoff_gps_by_uav']=homes
    heights={1:59,2:56,3:53,4:50,5:47,6:44}
    flyable=Polygon(a['points_m'],holes=a['excluded_polygons_m'])
    local=lambda q:[(q[1]-p['latitude'])*p['north_m_per_degree'],-(q[0]-p['longitude'])*p['west_m_per_degree']]
    xy=lambda q:(-q[1],q[0])
    route_local={};minimum_home=math.inf;minimum_boundary=math.inf
    for uid,item in plan['planned_uavs'].items():
        r=rebase_gps_routes_for_takeoff(a,item['task'],homes[uid]);item['task']['transit_routes']=r
        item['target_altitude_m']=heights[int(uid)]
        entry=[local(q) for q in r['entry_path']]
        last=next(route for route in r['return_paths'] if route['source_uav_id']==int(uid)
                  and route['waypoint_index']==len(item['task']['waypoints_m']))
        route_local[int(uid)]=(entry,[local(q) for q in last['path']])
        for route in [r['entry_path']]+[v['path'] for v in r['return_paths']]:
            line=LineString([local(q) for q in route])
            assert flyable.covers(line)
            minimum_boundary=min(minimum_boundary,line.distance(flyable.boundary))
            minimum_home=min(minimum_home,*(line.distance(Point(pos)) for other,pos in offsets.items() if str(other)!=uid))
    assert minimum_home>=2.5 and minimum_boundary>=5
    plan.update(example_only=True,flight_altitude_plan='around54m',
                description='示意起降布局，不是实测GPS；UAV2位于UAV1原直线连接上，用真实运行时函数生成全部路线。')
    plan['example_metrics']=dict(min_other_home_distance_m=minimum_home,min_boundary_distance_m=minimum_boundary,
        recipient_count=6,return_paths_per_recipient=75)
    safe=flyable.buffer(-5)
    near,_=nearest_points(LineString(route_local[1][0]),Point(offsets[2]))
    plan['example_plot_data']=dict(offsets=offsets,routes=route_local,
        safe_rings=[list(ring.coords) for ring in [safe.exterior,*safe.interiors]],
        uav1_nearest=list(near.coords)[0],uav1_clearance_m=LineString(route_local[1][0]).distance(Point(offsets[2])))
    path=ROOT/'docs/xuchang_boot_home_example.json'
    path.write_text(json.dumps(plan,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
    print(json.dumps(plan['example_metrics'],ensure_ascii=False))
    return plan


def plot(plan):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle
    from matplotlib.lines import Line2D
    plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
    plt.rcParams['axes.unicode_minus']=False
    a=plan['search_area'];data=plan['example_plot_data'];docs=ROOT/'docs'
    json_path=docs/'xuchang_boot_home_example.json'
    offsets={int(uid):pos for uid,pos in data['offsets'].items()}
    route_local={int(uid):paths for uid,paths in data['routes'].items()}
    heights={int(uid):item['target_altitude_m'] for uid,item in plan['planned_uavs'].items()}
    minimum_home=plan['example_metrics']['min_other_home_distance_m']
    minimum_boundary=plan['example_metrics']['min_boundary_distance_m']
    xy=lambda q:(-q[1],q[0])
    colors=['#1764cc','#178749','#ed861b','#8446bd','#008fa8','#cf3653']
    fig,(overview,detail)=plt.subplots(1,2,figsize=(15.4,8.8),gridspec_kw={'width_ratios':[1.05,1]})
    fig.patch.set_facecolor('#f6f8fc')
    fig.suptitle('许昌试飞场地（小）｜各机起降点避让案例',fontsize=21,fontweight='bold',y=.978,color='#12253b')
    fig.text(.5,.925,'示例起降点，非现场实测GPS · 现有子区 / 75个侦察航点 / 高度方案不变',ha='center',fontsize=11,color='#526279')
    for ax in (overview,detail):
        ax.set_facecolor('white');ax.set_aspect('equal');ax.grid(alpha=.18)
        ax.set_xlabel('东向（米） →',fontsize=11);ax.set_ylabel('北向（米） ↑',fontsize=11)
        for spine in ax.spines.values():spine.set_color('#bbc7d5')
    for uid,item in plan['planned_uavs'].items():
        region=item['task']['polygon_m'];x,y=zip(*map(xy,region))
        overview.fill(x,y,color=colors[int(uid)-1],alpha=.09)
        overview.plot([*x,x[0]],[*y,y[0]],color=colors[int(uid)-1],lw=.7,alpha=.6)
        scans=item['task']['waypoints_m'];x,y=zip(*map(xy,scans))
        overview.scatter(x,y,c=colors[int(uid)-1],s=9,alpha=.6)
    for ring in a['excluded_polygons_m']:
        x,y=zip(*map(xy,ring));overview.fill(x,y,color='#b7555b',alpha=.28,hatch='///',edgecolor='#a4313d')
    outline=[*a['points_m'],a['points_m'][0]];overview.plot(*zip(*map(xy,outline)),c='#384a60',lw=1.5)
    for ring in data['safe_rings']:overview.plot(*zip(*map(xy,ring)),c='#b4993b',lw=.8,ls='--')
    for uid,(entry,returned) in route_local.items():
        overview.plot(*zip(*map(xy,entry)),c=colors[uid-1],lw=1.5)
        overview.plot(*zip(*map(xy,returned)),c=colors[uid-1],lw=1.6,ls=(0,(5,3)),alpha=.85)
        overview.scatter(*xy(offsets[uid]),c=colors[uid-1],marker='s',s=25,edgecolors='white',zorder=8)
    overview.set_title('全场：进场实线 · 本机最后航点返航虚线',fontsize=12,pad=12)
    overview.text(235,65,'中央禁飞区',ha='center',color='#8a2731',fontsize=10)
    overview.add_patch(plt.Rectangle((-23,-23),50,55,fill=False,ec='#142c46',lw=1.5))
    overview.annotate('右图放大起降区',xy=(27,32),xytext=(80,-45),fontsize=10,
                      arrowprops=dict(arrowstyle='->',color='#526279'),color='#526279')
    detail.set_title('UAV1绕开UAV2降落点（半径2.5米）',fontsize=12,pad=12)
    # 原直线仅作对比，不包含在下发路线中。
    detail.plot(*zip(*map(xy,[offsets[1],[70,-45]])),color='#9ba5b3',ls=':',lw=1.7)
    entry,returned=route_local[1]
    detail.plot(*zip(*map(xy,entry)),c=colors[0],lw=2.5,label='UAV1实际进场')
    detail.plot(*zip(*map(xy,returned)),c='#e97321',lw=2,ls=(0,(4,3)),label='UAV1最终返航')
    for uid,pos in offsets.items():
        x,y=xy(pos)
        detail.add_patch(Circle((x,y),2.5,fc='#e64f5720',ec='#b63340',lw=1.1))
        detail.scatter(x,y,c=colors[uid-1],marker='s',s=56,edgecolor='white',zorder=10)
        detail.annotate('UAV%d\n%dm'%(uid,heights[uid]),(x,y),xytext=(-29 if uid in (3,4,6) else 7,9),
                        textcoords='offset points',fontsize=9,color=colors[uid-1],fontweight='bold')
    nearest=xy(data['uav1_nearest']);obstacle=xy(offsets[2])
    measured=data['uav1_clearance_m'];detail.plot([nearest[0],obstacle[0]],[nearest[1],obstacle[1]],c='#ac2d3b',lw=1.3)
    detail.annotate('最近间距 %.2fm'%measured,xy=nearest,xytext=(11,22),fontsize=11,color='#ac2d3b',
                    arrowprops=dict(arrowstyle='->',color='#ac2d3b'),bbox=dict(boxstyle='round,pad=.35',fc='white',ec='#eed8da'))
    detail.set_xlim(-23,27);detail.set_ylim(-23,32)
    detail.legend(handles=[Line2D([0],[0],color=colors[0],lw=2,label='实际进场'),
        Line2D([0],[0],color='#e97321',ls='--',lw=2,label='实际返航'),
        Line2D([0],[0],color='#9ba5b3',ls=':',label='原直线（仅对比）')],loc='lower right',fontsize=9,framealpha=.95)
    fig.legend(handles=[Line2D([0],[0],color=c,lw=2,label='UAV%d'%(i+1)) for i,c in enumerate(colors)],
               loc='lower center',bbox_to_anchor=(.29,.09),ncol=3,fontsize=10,frameon=False)
    fig.text(.5,.055,'全部进返场校验：距其他降落点最小 %.2fm（要求≥2.5m）｜距外边界/禁飞区最小 %.2fm（要求≥5m）'%(minimum_home,minimum_boundary),
             ha='center',fontsize=11,color='#163a54')
    fig.text(.5,.022,'每机仍收到全机队75个航点到自己降落点的返航路线；图中仅画各机最后航点返航。2.5米指与落点的水平间距。',
             ha='center',fontsize=10,color='#596a7b')
    fig.subplots_adjust(left=.065,right=.97,top=.87,bottom=.21,wspace=.21)
    image_path=docs/'xuchang_boot_home_example.png';fig.savefig(image_path,dpi=160,facecolor=fig.get_facecolor());plt.close(fig)
    print(json.dumps(dict(image=str(image_path),json=str(json_path),**plan['example_metrics']),ensure_ascii=False))

if __name__=='__main__':
    plan=(json.loads((ROOT/'docs/xuchang_boot_home_example.json').read_text(encoding='utf-8'))
          if '--plot-only' in sys.argv else build_data())
    if '--data-only' not in sys.argv:
        plot(plan)
