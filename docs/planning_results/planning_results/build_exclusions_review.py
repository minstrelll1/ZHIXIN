#!/usr/bin/env python3
"""Recreate provisional, manually traced exclusion geometry and review figure.

No network source, fabricated survey, or assumed anchor-radius is used.
The historical interpretation remains subject to user confirmation.
"""
from pathlib import Path
import hashlib
import json
import math
import os
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Point, Polygon, shape, mapping
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parent
INPUT = ROOT / 'inputs' / '02_规划输入.json'
RASTER = ROOT / 'inputs' / '05_历史底图.jpg'

# Pixel coordinates of visible paths/banks, digitized manually on the supplied raster.
# Pixel convention: image extent [0,width] x [0,height], not [0,width-1].
TRACES = [
    ('southeast_exclusion_anchor',
     [[668,1184],[754,1188],[782,1192],[796,1201],[791,1387],
      [750,1380],[725,1343],[696,1310],[668,1274]],
     '东南植被地块候选：沿北侧横路、东侧道路、南侧场界小径及西侧折转斜路圈定。'
     '定位点只确定附近位置，不能证明用户所指范围恰为整个地块；选择完整历史地块是显式草案假设。'),
    ('pond_exclusion_anchor',
     [[460,1264],[514,1269],[524,1276],[522,1340],[514,1346],
      [456,1340],[453,1332],[456,1271]],
     '池塘候选：沿可见近矩形水面外围岸线/窄堤的转折圈定，包含定位点。'
     '阴影、水陆交界、2017年至今水位及地物变化使现状边界不可确认。'),
]

def review_fonts():
    """Prefer an installed CJK font; use ASCII labels if none can be loaded.

    Font locations are optional platform hints, never required input paths.
    PLANNING_REVIEW_FONT can name a local CJK font. PLANNING_REVIEW_ASCII=1
    explicitly exercises the font-independent English fallback.
    """
    sizes=(22,18,30)
    candidates=[]
    custom=os.environ.get('PLANNING_REVIEW_FONT')
    if custom:
        candidates.append(custom)
    candidates += [str(ROOT/'assets'/'fonts'/'NotoSansCJK-Regular.ttc'),
                   'NotoSansCJK-Regular.ttc','NotoSansCJKsc-Regular.otf',
                   'NotoSansSC-Regular.ttf','msyh.ttc','simhei.ttf',
                   'PingFang.ttc','Hiragino Sans GB.ttc']
    windows_root=os.environ.get('SystemRoot') or os.environ.get('WINDIR')
    if windows_root:
        candidates += [str(Path(windows_root)/'Fonts'/name)
                       for name in ('msyh.ttc','simhei.ttf','simsun.ttc')]
    candidates += [str(Path.home()/'Library'/'Fonts'/'NotoSansCJK-Regular.ttc'),
                   '/System/Library/Fonts/PingFang.ttc',
                   '/System/Library/Fonts/Hiragino Sans GB.ttc',
                   '/System/Library/Fonts/Supplemental/Songti.ttc',
                   '/Library/Fonts/Arial Unicode.ttf',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                   '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
                   '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf']
    if os.environ.get('PLANNING_REVIEW_ASCII')!='1':
        for candidate in candidates:
            try:
                return (*[ImageFont.truetype(candidate,size) for size in sizes],True)
            except (OSError,ValueError):
                pass
    # Pillow commonly bundles/finds DejaVuSans. ASCII remains renderable even
    # on older Pillow versions whose built-in bitmap font only supports Latin.
    try:
        fonts=[ImageFont.truetype('DejaVuSans.ttf',size) for size in sizes]
    except (OSError,ValueError):
        try:
            fonts=[ImageFont.load_default(size=size) for size in sizes]
        except TypeError:
            fonts=[ImageFont.load_default() for size in sizes]
    return (*fonts,False)

def main():
    inp = json.loads(INPUT.read_text(encoding='utf-8'))
    meta = inp['historical_basemap']
    proj = inp['final_field']['projection']
    original = Image.open(RASTER).convert('RGB')
    width, height = original.size
    south, west = meta['image_bounds_southwest_northeast_lat_lon'][0]
    north, east = meta['image_bounds_southwest_northeast_lat_lon'][1]
    def merc(lat):
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    mn, ms = merc(north), merc(south)
    def px_to_ll(p):
        x, y = p
        lon = west + x / width * (east-west)
        lat = math.degrees(2*math.atan(math.exp(mn-y/height*(mn-ms)))-math.pi/2)
        return [round(lon,12), round(lat,12)]
    def ll_to_px(lon,lat):
        return [(lon-west)/(east-west)*width, (mn-merc(lat))/(mn-ms)*height]
    def to_nw(lon,lat,z=None):
        return ((lat-proj['latitude'])*proj['north_m_per_degree'],
                -(lon-proj['longitude'])*proj['west_m_per_degree'])
    def to_ll(n,w,z=None):
        return (proj['longitude']-w/proj['west_m_per_degree'],
                proj['latitude']+n/proj['north_m_per_degree'])
    boundary = transform(to_nw, shape(inp['final_field']['boundary_geometry_wgs84']))
    exempt = transform(to_nw, shape(inp['final_field']['terrain']['coverage_exempt_geometry_wgs84']))
    anchors = {e['id']: e for e in inp['final_field']['strict_scan_exclusions']}
    result = {
        'artifact_type': 'independent_historical_exclusion_draft',
        'status': 'draft_requires_user_confirmation',
        'source_date': meta['date'],
        'source_date_basis': '02_规划输入.json historical_basemap.date; not independently authenticated from satellite provider',
        'source': 'inputs/05_历史底图.jpg, user-supplied historical raster',
        'source_sha256': hashlib.sha256(RASTER.read_bytes()).hexdigest(),
        'input_sha256': hashlib.sha256(INPUT.read_bytes()).hexdigest(),
        'is_current_survey': False,
        'is_user_confirmed_boundary': False,
        'method': 'Manual path/bank pixel tracing; linear longitude and inverse interpolated WebMercator latitude from supplied image bounds; fixed field projection for metric checks.',
        'image_registration': dict(meta, width_px=width, height_px=height,
                                   pixel_extent_convention='[0,width] x [0,height]; upper-left origin'),
        'manual_pixel_vertices_are_reproducible_inputs': True,
        'exclusions': [],
        'sensitivity_is_not_an_authorized_boundary_change': True,
        'limitations': [
            '真实边界未经用户确认；规划只能作为完整可复算草案，不能声明满足真实禁扫要求或可直接飞行。',
            '日期来自准备方提供的输入元数据；原始影像服务商、拍摄真实性和绝对配准误差无独立核验材料。',
            '两个锚点未定义禁扫范围；本文件没有把附近偷换成固定半径圆。',
            '道路与岸线仅为可审查的历史地物代理，±10m敏感性不是测量误差置信区间。',
            '两个候选均按完整扫描圆不得相交处理；它们不是自动禁飞区。',
            '2021森林/水体覆盖豁免与这些强禁扫多边形分别计算，不能抵消强禁扫限制。',
        ],
    }
    polygons = []
    for ident, pix, interpretation in TRACES:
        ll = [px_to_ll(p) for p in pix]
        geom = {'type':'Polygon', 'coordinates':[ll+[ll[0]]]}
        poly = transform(to_nw,shape(geom))
        assert poly.is_valid and boundary.buffer(0.00001).covers(poly)
        a = anchors[ident]
        ap = Point(*to_nw(a['longitude'],a['latitude']))
        assert poly.covers(ap)
        polygons.append(poly)
        sensitivity = []
        for delta in (-10.0,0.0,10.0):
            trial = poly.buffer(delta,quad_segs=128).intersection(boundary)
            sensitivity.append({
                'boundary_offset_m':delta,
                'area_within_boundary_m2':round(trial.area,6),
                'additional_area_removed_after_terrain_exemption_m2':round(trial.difference(exempt).area,6),
                'upper_scan_centers_forbidden_within_boundary_m2':round(trial.buffer(30.0,quad_segs=128).intersection(boundary).area,6),
                'lower_scan_centers_forbidden_within_boundary_m2':round(trial.buffer(50.0,quad_segs=128).intersection(boundary).area,6),
            })
        result['exclusions'].append({
            'id':ident, 'kind':'scan_disk_forbidden',
            'geometry_wgs84':geom, 'geometry_status':'provisional',
            'source':'inputs/05_历史底图.jpg; historical_basemap registration in inputs/02_规划输入.json',
            'source_date':meta['date'], 'source_sha256':result['source_sha256'],
            'manual_pixel_vertices':pix,
            'anchor_wgs84_lon_lat':[a['longitude'],a['latitude']],
            'anchor_pixel_xy':ll_to_px(a['longitude'],a['latitude']),
            'polygon_m':[[round(n,6),round(w,6)] for n,w in list(poly.exterior.coords)[:-1]],
            'area_m2':round(poly.area,6),
            'anchor_inside':poly.covers(ap),
            'geometry_valid':poly.is_valid,
            'outside_boundary_area_m2':round(poly.difference(boundary).area,6),
            'flight_prohibited':False, 'radius_m':None,
            'interpretation':interpretation,
            'confirmation_required':True,
            'sensitivity':sensitivity,
        })
    both=unary_union(polygons)
    result['combined_metrics']={
        'candidate_union_area_m2':round(both.area,6),
        'overlap_area_m2':round(sum(p.area for p in polygons)-both.area,6),
        'additional_area_removed_after_terrain_exemption_m2':round(both.difference(exempt).area,6),
        'draft_effective_area_m2':round(boundary.difference(unary_union([exempt,both])).area,6),
    }
    (ROOT/'exclusions_draft.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

    # Review panel, using only final JSON traces and supplied registration.
    font,small,big,chinese=review_fonts()
    def label(zh,en):return zh if chinese else en
    review=Image.new('RGB',(1600,1380),'#101b29')
    draw=ImageDraw.Draw(review)
    draw.text((30,16),label('强禁扫候选边界审查｜独立草案，待用户确认','Strict scan exclusion review | Independent draft, awaiting confirmation'),font=big,fill='white')
    draw.text((30,66),label('历史影像日期：2017-07-24（输入元数据）  坐标：WGS84  原图：1800 × 1800','Historical image: 2017-07-24 (supplied metadata) | WGS84 | Original: 1800 x 1800'),font=font,fill='#d4dfef')
    draw.text((30,103),label('红色：东南地块候选   青色：池塘候选   黄色：10点外边界   十字：用户定位点','Red: southeast draft | Cyan: pond draft | Yellow: 10-point boundary | Cross: supplied anchor'),font=font,fill='#d4dfef')
    left,top,scale=425,890,2.4
    crop=original.crop((425,890,840,1415)).resize((996,1260),Image.Resampling.NEAREST)
    # Compose a smaller enough image within the panel.
    review.paste(crop,(20,150))
    overlay=Image.new('RGBA',review.size,(0,0,0,0)); od=ImageDraw.Draw(overlay)
    def screen(p):return (20+(p[0]-left)*scale,150+(p[1]-top)*scale)
    boundary_px=[ll_to_px(lon,lat) for lat,lon in inp['final_field']['boundary_wgs84_lat_lon']]
    od.line([screen(p) for p in boundary_px]+[screen(boundary_px[0])],fill=(255,230,80,255),width=3)
    colors=[(255,98,90,255),(51,228,235,255)]
    for j,ex in enumerate(result['exclusions']):
        pts=[screen(p) for p in ex['manual_pixel_vertices']]
        od.polygon(pts,fill=colors[j][:3]+(45,))
        od.line(pts+[pts[0]],fill=colors[j],width=5)
        for k,p in enumerate(pts):
            od.ellipse((p[0]-3,p[1]-3,p[0]+3,p[1]+3),fill=colors[j])
            od.text((p[0]+5,p[1]-16),str(k+1),font=small,fill=colors[j])
        ax,ay=screen(ex['anchor_pixel_xy'])
        od.line((ax-13,ay,ax+13,ay),fill=(255,255,255,255),width=3)
        od.line((ax,ay-13,ax,ay+13),fill=(255,255,255,255),width=3)
        od.text((ax+16,ay-14),'SE' if j==0 else 'POND',font=font,fill=(255,255,255,255))
    review=Image.alpha_composite(review.convert('RGBA'),overlay).convert('RGB');draw=ImageDraw.Draw(review)
    y=175
    for j,ex in enumerate(result['exclusions']):
        color=colors[j]
        draw.text((1050,y),label('东南地块候选','Southeast candidate') if j==0 else label('池塘候选','Pond candidate'),font=big,fill=color);y+=45
        for line in [label(f"{len(ex['manual_pixel_vertices'])} 个描绘顶点；面积 {ex['area_m2']:,.1f} ㎡",f"{len(ex['manual_pixel_vertices'])} vertices; {ex['area_m2']:,.1f} sq m"),
                     label('依据：2017 影像可见道路/岸线','2017 image: visible paths / banks'),
                     label('定位点位于候选多边形内','Supplied anchor is inside polygon'),
                     label('圈定范围未获用户确认','Boundary is not user-confirmed'),
                     label('非锚点 30m / 50m 缓冲圆','No assumed 30m / 50m anchor circle')]:
            draw.text((1050,y),line,font=small,fill='#d4dfef');y+=33
        y+=28
    for line in [label('边界敏感性（不是误差置信区间）','Boundary sensitivity, not error bounds'),
                 label('另对候选边界内缩/外扩 10m 重算。','Also checked: inward / outward 10m.'),
                 label('面积与禁扫圆心限制见 JSON。','See JSON for area and center limits.'),
                 '',
                 label('配准：经度线性；纬度按墨卡托反算。','Linear longitude / Mercator latitude.'),
                 label('没有现状测绘、实飞或许可保证。','No current survey or flight assurance.'),
                 label('黄色外边界亦来自固定输入坐标。','Yellow boundary uses supplied points.'),
                 '',
                 label('使用前需确认两个真实禁扫范围。','Confirm both actual exclusion areas.'),
                 label('扫描圆半径另按上层30m/下层50m。','Scan radius: upper 30m / lower 50m.'),
                 label('禁扫区域不自动禁止非扫描飞越。','Scan exclusion is not a no-fly zone.')]:
        draw.text((1050,y),line,font=small,fill='#e4c987');y+=34
    review.save(ROOT/'exclusions_review.png')
    notes=[
        '两个强禁扫候选边界：独立历史影像草案，非已确认主方案边界',
        '',
        f"来源：inputs/05_历史底图.jpg；SHA256={result['source_sha256']}",
        '来源日期：2017-07-24，取自02_规划输入.json historical_basemap.date。',
        '没有外部影像服务商核验材料，不能把来源日期或配准当作现状测绘证据。',
        '圈定脚本：build_exclusions_review.py。手工像素顶点作为固定输入保存在脚本及JSON中。',
        '坐标转换：longitude=west+(px/width)*(east-west)；MercatorY从north向south线性插值后反算latitude。',
        '像素定义：[0,width]×[0,height]，左上角原点；经纬度固定保留12位小数后再次验证。',
        '',
    ]
    for ex in result['exclusions']:
        notes += [ex['id'],ex['interpretation'],
                  f"面积 {ex['area_m2']:.6f} ㎡；有效几何={ex['geometry_valid']}；包含锚点={ex['anchor_inside']}。",
                  '外环在GeoJSON内闭合；polygon_m按[北,西]保存且不重复首点。',
                  '边界内缩/外扩敏感性：']
        for s in ex['sensitivity']:
            notes.append(f"  偏移{s['boundary_offset_m']:+.0f}m：多边形面积{s['area_within_boundary_m2']:.3f}㎡；扣现有地类豁免后新增扣除{s['additional_area_removed_after_terrain_exemption_m2']:.3f}㎡；上/下层禁设圆心范围面积{s['upper_scan_centers_forbidden_within_boundary_m2']:.3f}/{s['lower_scan_centers_forbidden_within_boundary_m2']:.3f}㎡。")
        notes += ['']
    notes += ['合并指标：'+json.dumps(result['combined_metrics'],ensure_ascii=False),'']+result['limitations']+[
        '',
        '依赖：Python 3、Pillow、Shapely；运行：python3 build_exclusions_review.py。',
        '审查图自动查找Windows/macOS/Linux中文字体；缺少时使用英文标签和Pillow备用字体，无必需的主机字体路径。可用PLANNING_REVIEW_FONT指定本地中文字体，或PLANNING_REVIEW_ASCII=1强制英文。',
        '须先由用户或现状勘测确认边界；确认后重新运行规划及独立验证。',
        '面积敏感性不等于已重新优化三个场景的覆盖率变化，不宣称统计意义上的定位精度。',
    ]
    (ROOT/'exclusions_notes.txt').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    print(json.dumps(result['combined_metrics'],ensure_ascii=False))
    for ex in result['exclusions']:
        print(ex['id'],ex['area_m2'])

if __name__=='__main__':
    main()
