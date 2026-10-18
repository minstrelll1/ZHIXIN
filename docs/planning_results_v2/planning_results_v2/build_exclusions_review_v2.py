#!/usr/bin/env python3
"""Draw v2 boundary and historical-landcover audits from frozen JSON inputs."""
from pathlib import Path
import json
import math
import os
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parent
def read(name):return json.loads((ROOT/name).read_text(encoding='utf-8'))

def fonts():
    paths=[]
    if os.environ.get('PLANNING_REVIEW_FONT'):paths.append(os.environ['PLANNING_REVIEW_FONT'])
    paths += ['NotoSansCJK-Regular.ttc','msyh.ttc','simhei.ttf','PingFang.ttc',
              '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/System/Library/Fonts/PingFang.ttc',
              '/System/Library/Fonts/Hiragino Sans GB.ttc',
              '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf']
    windir=os.environ.get('SystemRoot') or os.environ.get('WINDIR')
    if windir:paths += [str(Path(windir)/'Fonts'/name) for name in ('msyh.ttc','simhei.ttf','simsun.ttc')]
    if os.environ.get('PLANNING_REVIEW_ASCII')!='1':
        for path in paths:
            try:return [ImageFont.truetype(path,s) for s in (19,24,32)],True
            except (OSError,ValueError):pass
    try:return [ImageFont.truetype('DejaVuSans.ttf',s) for s in (19,24,32)],False
    except (OSError,ValueError):
        try:return [ImageFont.load_default(size=s) for s in (19,24,32)],False
        except TypeError:return [ImageFont.load_default() for s in (19,24,32)],False

def main():
    data=read('inputs/02_规划输入_v2.json');ex=read('exclusions_draft.json');audit=read('terrain_mask_audit.json')
    original=Image.open(ROOT/data['historical_basemap']['file']).convert('RGB')
    meta=data['historical_basemap'];(south,west),(north,east)=meta['image_bounds_southwest_northeast_lat_lon']
    merc=lambda lat:math.log(math.tan(math.pi/4+math.radians(lat)/2))
    top_y,bottom_y=merc(north),merc(south)
    def pixel(lon,lat):
        return ((lon-west)/(east-west)*original.width,(top_y-merc(lat))/(top_y-bottom_y)*original.height)
    (small,normal,big),chinese=fonts()
    def label(zh,en):return zh if chinese else en
    def make_canvas(title,subtitle):
        image=Image.new('RGBA',(1750,1370),'#102031');d=ImageDraw.Draw(image)
        d.text((28,18),title,font=big,fill='white');d.text((28,67),subtitle,font=normal,fill='#cbd7e5')
        return image
    def panel(image,box,screen_box):
        x,y,w,h=screen_box
        image.paste(original.crop(box).resize((w,h),Image.Resampling.BILINEAR),(x,y))
        return lambda lon,lat:(x+(pixel(lon,lat)[0]-box[0])/(box[2]-box[0])*w,
                               y+(pixel(lon,lat)[1]-box[1])/(box[3]-box[1])*h)
    def polygons(geom):
        if geom['type']=='Polygon':return [geom['coordinates']]
        if geom['type']=='MultiPolygon':return geom['coordinates']
        if geom['type']=='GeometryCollection':
            return [p for g in geom['geometries'] for p in polygons(g)]
        return []
    def geom(image,g,coords,color,opacity=0,width=3):
        for rings in polygons(g):
            if not rings:continue
            pix=[[coords(*p[:2]) for p in ring] for ring in rings]
            if opacity:
                mask=Image.new('L',image.size,0);md=ImageDraw.Draw(mask)
                md.polygon(pix[0],fill=opacity)
                for hole in pix[1:]:md.polygon(hole,fill=0)
                overlay=Image.new('RGBA',image.size,color);overlay.putalpha(mask)
                image.alpha_composite(overlay)
            d=ImageDraw.Draw(image)
            for ring in pix:
                if len(ring)>1:d.line(ring,fill=color,width=width)
    def annotate(image,texts,x,y,font=normal,color='#d7e2ee',step=38):
        d=ImageDraw.Draw(image)
        for t in texts:d.text((x,y),t,font=font,fill=color);y+=step
    new_forest=ex['exclusions'][0];pond=ex['exclusions'][1]
    boundary=data['final_field']['boundary_geometry_wgs84']
    old=audit['old_strict_southeast_geometry_wgs84']
    new=audit['new_strict_southeast_geometry_wgs84']
    yellow=(255,225,72,255);red=(255,85,100,255);orange=(255,174,65,255);cyan=(49,224,235,255)

    image=make_canvas(label('v2 强禁扫边界审查｜新树林整体替换旧候选','v2 strict exclusions | Forest geometry fully replaces old candidate'),
        label('底图2017-07-24；树林8个用户点来自本次修订，Q补点由原编号边界正交推导。','Basemap: 2017-07-24; eight revised user points; Q connectors derived on original numbered edges.'))
    full=panel(image,(425,890,840,1415),(20,120,830,1050))
    geom(image,boundary,full,yellow,0,3);geom(image,old,full,orange,40,4)
    geom(image,new,full,red,90,4);geom(image,pond['geometry_wgs84'],full,cyan,65,3)
    d=ImageDraw.Draw(image)
    ring=new['coordinates'][0][:-1]
    names=[f'P{i}' for i in range(1,9)]+['Q S','V9','Q E']
    for name,p in zip(names,ring):
        x,y=full(*p);d.ellipse((x-3,y-3,x+3,y+3),fill='white');d.text((x+7,y-20),name,font=small,fill='white')
    for e in ex['exclusions']:
        x,y=full(*e['anchor_wgs84_lon_lat']);d.line((x-10,y,x+10,y),fill='white',width=3);d.line((x,y-10,x,y+10),fill='white',width=3)
    details=[
        label('红色：新树林 4,935.39 ㎡','Red: replacement forest 4,935.39 sq m'),
        label('橙色：旧候选 18,147.41 ㎡（已移除）','Orange: old 18,147.41 sq m (replaced)'),
        label('青色：池塘 4,737.85 ㎡（仍待确认）','Cyan: pond 4,737.85 sq m (unconfirmed)'),
        label('黄色：固定10点飞行外边界','Yellow: fixed 10-point field boundary'),
        '',
        label('新树林不是旧多边形的缩放或内缩。','Replacement is not a scaled/buffered old polygon.'),
        label('其中 1,953.13 ㎡位于旧候选之外。','It includes 1,953.13 sq m outside the old candidate.'),
        '',
        label('P1…P8：保留用户坐标原值。','P1...P8: preserve the exact user coordinates.'),
        label('Q南：P8正交投影至原8→9南界。','Q S: project P8 onto original south edge 8-9.'),
        label('Q东：P1正交投影至原9→10东界。','Q E: project P1 onto original east edge 9-10.'),
        label('补点是开发对话推导，非用户实测。','Q points are derived, not user-measured.'),
        label('闭合：P1…P8→Q南→原9→Q东→P1。','Close: P1...P8 -> Q S -> V9 -> Q E -> P1.'),
        '',
        label('三场景半径：上层50m、下层30m。','All scenes: upper radius 50m; lower radius 30m.'),
        label('完整扫描圆禁止触及这两个禁扫区。','Full scan disks must avoid both exclusion areas.'),
        label('强禁扫并不自动禁止非扫描飞越。','A scan exclusion is not automatically a no-fly zone.'),
        '',
        label('来源日期分开保留：','Source dates retained separately:'),
        label('树林：2026-10-17 修订用户点+推导。','Forest: 2026-10-17 revision points + derivation.'),
        label('池塘：2017-07-24 历史影像草案。','Pond: 2017-07-24 historical-image draft.'),
        label('本图不是现状测绘或实飞许可。','This figure is not a current survey or flight approval.')]
    annotate(image,details,900,140,font=normal,step=39)
    annotate(image,[label('完整坐标、原编号、投影与补点误差见 exclusions_draft.json；可复算输入见 merge_inputs.py。',
        'Coordinates, original IDs, projection and connector residuals: exclusions_draft.json; rebuild: merge_inputs.py.'),
        label('历史地类归属尚未确认；主方案保留旧mask，详见 terrain_mask_review.png / terrain_mask_audit.json。',
        'Historical pixel ownership is unresolved; the primary mask is retained. See terrain_mask_review.png / terrain_mask_audit.json.')],28,1215,font=small,step=40)
    image.convert('RGB').save(ROOT/'exclusions_review.png')

    image=make_canvas(label('历史地类局部归属审查｜主方案保留，局部修正仅为敏感性','Historical landcover ownership audit | Retain primary mask; local correction is hypothetical'),
        label('2021森林/水体分类叠加2017底图及新树林；不能据此证明2026现状或全部像元属于同一片树林。',
              '2021 forest/water classes on 2017 imagery with the revised forest; this does not establish current conditions or ownership.'))
    raw=data['final_field']['terrain']
    left=panel(image,(425,890,840,1415),(20,125,830,1050))
    geom(image,raw['forest_geometry_wgs84'],left,(129,222,111,255),72,2)
    geom(image,raw['water_geometry_wgs84'],left,(56,177,255,255),110,4)
    geom(image,raw['coverage_exempt_geometry_wgs84'],left,(241,227,93,255),55,2)
    geom(image,old,left,orange,0,4);geom(image,new,left,red,10,4)
    geom(image,audit['stale_exemption_candidate_geometry_wgs84'],left,(255,74,216,255),145,3)
    geom(image,boundary,left,(255,255,255,255),0,2)
    details=[
        label('绿色：2021原始森林分类','Green: original 2021 forest classes'),
        label('蓝色：2021原始水体分类','Blue: original 2021 water classes'),
        label('黄色：原已处理覆盖豁免mask','Yellow: preprocessed historical exemption mask'),
        label('橙线：已替换的旧东南候选边界','Orange outline: replaced old southeast candidate'),
        label('红线：新树林强禁扫区','Red outline: replacement strict forest exclusion'),
        label('品红：旧候选内、新树林外的森林豁免','Magenta: old-candidate forest exemption outside new forest'),
        '',
        label('候选过时森林豁免：1,836.737678 ㎡','Potential stale forest exemption: 1,836.737678 sq m'),
        label('这部分的“同片树林”归属尚未确认。','Its membership in the same forest is unconfirmed.'),
        '',
        label('主方案：保留所有原mask。','Primary: retain the complete historical mask.'),
        label(f"有效目标面积：{audit['variants'][0]['effective_area_m2']:,.6f} ㎡",f"Effective area: {audit['variants'][0]['effective_area_m2']:,.6f} sq m"),
        '',
        label('局部修正假设：仅去掉品红豁免。','Hypothesis: remove only the magenta exemption.'),
        label(f"有效目标面积：{audit['variants'][1]['effective_area_m2']:,.6f} ㎡",f"Effective area: {audit['variants'][1]['effective_area_m2']:,.6f} sq m"),
        label('该假设未用于主规划，不声称已确认。','Not selected for the primary plan; not confirmed.'),
        '',
        label('其他森林、水体与原处理规则全部保留。','All other forest, water and prior processing are retained.'),
        label('已处理mask不再次全场内缩。','The preprocessed mask is not globally buffered again.'),
        label('新树林内部仍由完整强禁扫区约束。','The entire new forest remains a strict scan exclusion.'),
        '',
        label('两个敏感性方案保留完整GeoJSON。','Both variants include complete GeoJSON geometry.'),
        label('来源、面积与检查见terrain_mask_audit.json。','See terrain_mask_audit.json for sources, areas and checks.')]
    annotate(image,details,900,140,font=small,step=39)
    annotate(image,[label('底图日期来自输入元数据；WorldCover分类约10m精度。该叠图只能审查归属假设，不能代替现状测绘。',
        'Basemap date comes from supplied metadata; WorldCover resolution is about 10m. The overlay does not replace a current survey.'),
        label('主方案有效面积约111256.33㎡与修订核对基准一致；局部修正敏感性增加1836.74㎡。',
        'Primary effective area matches the revision baseline (~111256.33 sq m); local-correction sensitivity adds ~1836.74 sq m.')],28,1215,font=small,step=40)
    image.convert('RGB').save(ROOT/'terrain_mask_review.png')
    print('Wrote exclusions_review.png and terrain_mask_review.png from frozen v2 JSON; no inputs modified.')

if __name__=='__main__':main()
