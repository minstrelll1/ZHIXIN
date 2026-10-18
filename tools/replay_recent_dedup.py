"""只读真实回传，复制到隔离目录回放；实际submit方法的HTTP传输始终被拦截。"""
import argparse
from collections import Counter
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'competition_backend'), str(ROOT/'src/su17_image_transfer/src')]
from competition_backend import subject1_reporting as report
from competition_shared.reporting_selection import save_reporting_selection
from competition_shared.recognition import optional_recognition_selection


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf8')


def check_ranking(document, audit, raw_document):
    """从原始候选独立核对代表、最终顺序及发送内容，避免只验证审计标签。"""
    originals = {(str(f['id']), f['_source_uav']): f for f in raw_document['features']}
    def quality(feature):
        props = feature['properties']; q = feature['_quality']
        return (q['detection_count'], int(q['tracking_success']), props.get('confidence', -1.0))
    ranks = []
    assert len(document['features']) == len(audit['quality_ranking'])
    for submitted, row in zip(document['features'], audit['quality_ranking']):
        source = originals[str(row['id']), row['source_uav']]
        props = submitted['properties']; source_props = source['properties']
        assert props['targetType'] == source_props['targetType'] == row['target_type']
        assert props['targetCategory'] == source_props['targetCategory'] == row['target_category']
        assert props.get('confidence', -1.0) == source_props.get('confidence', -1.0) == row['score']
        assert quality(source) == (row['detection_count'], int(row['tracking_success']), row['score'])
        ranks.append((int(props['targetType'] in ('车辆', '工事')),)+quality(source))
        if props['targetCategory'] == '固定':
            assert submitted['geometry'] == source['geometry']
            assert props['timestamp'] == source_props['timestamp']
        else:
            assert props['trackPoints'] == source_props['trackPoints'][:40]
            assert submitted['geometry']['coordinates'] == [p['coordinates'] for p in props['trackPoints']]
    assert ranks == sorted(ranks, reverse=True), '最终静动目标优先级顺序错误'
    for row in audit['merged']:
        if row['target_category'] == '固定':
            kept = originals[str(row['kept_id']), row['kept_source_uav']]
            merged = originals[str(row['merged_id']), row['source_uav']]
            assert quality(kept) >= quality(merged), '静目标重复组未选择质量最高的代表'
    expected = ['vehicle_or_building', 'detection_count', 'tracking_success', 'confidence']
    assert audit['static_quality_order'] == audit['moving_quality_order'] == expected
    assert audit['representative_quality_order'] == expected[1:]


def check_submit(reporter, prepared, document):
    """验证现有赛方multipart协议及冻结文件的字节一致性，绝不发往ENDPOINT。"""
    captured = {}
    class OfflineTransport:
        def open(self, request, timeout):
            assert request.full_url == report.ENDPOINT and request.get_method() == 'POST'
            assert timeout == 20
            mime = BytesParser(policy=default).parsebytes(
                ('Content-Type: '+request.get_header('Content-type')+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+request.data)
            part, = list(mime.iter_parts())
            assert part.get_param('name', header='content-disposition') == 'file'
            assert part.get_filename() == 'target-submission.json'
            raw = part.get_payload(decode=True)
            assert raw == reporter.draft(prepared['draft_id']).read_bytes()
            assert json.loads(raw.decode('utf8')) == document
            assert not request.has_header('Authorization')
            captured.update(offline_intercepted=True, method='POST', form_field='file',
                endpoint=request.full_url, filename=part.get_filename(), utf8_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(), validation=report.validate_document(document))
            response = io.BytesIO(b'{"offline_test":true,"accepted":true}')
            response.code = 200
            return response
    with patch.object(report.urllib.request, 'build_opener', return_value=OfflineTransport()):
        receipt = reporter.submit(prepared['draft_id'])
    assert receipt['http_status'] == 200 and receipt['state'] == 'http_received'
    captured['receipt_is_simulated'] = True
    return captured


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='2026-10-14')
    parser.add_argument('--received-root', type=Path, default=ROOT/'received_images')
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    source=args.received_root.resolve(); output=args.output.resolve()
    if source == output or source in output.parents:
        raise ValueError('回放输出必须在received_images以外，不能覆盖原始飞行结果')
    output.mkdir(parents=True, exist_ok=False)
    originals={}; missions=[]
    for session in sorted(source.iterdir()):
        if not session.is_dir() or not re.match(r'\d{4}-\d{2}-\d{2}_',session.name) or session.name[:10]<args.since:
            continue
        groups={}
        for path in sorted(session.glob('UAV*/*/*.json')):
            try: data=json.loads(path.read_text(encoding='utf8'))
            except (OSError,ValueError):continue
            if isinstance(data,dict) and str(data.get('mission_id','')).startswith('subject1-'):
                groups.setdefault(data['mission_id'],[]).append((path,data))
        for mission_id, rows in groups.items():
            missions.append((session,mission_id,rows))
    ids={mid for _,mid,_ in missions}; choices={}
    for path in sorted((ROOT/'ground_logs/sessions').glob('*/task_uav*.json')):
        try: task=json.loads(path.read_text(encoding='utf8'))
        except (OSError,ValueError):continue
        mid=task.get('mission_id')
        if mid not in ids:continue
        selection=optional_recognition_selection(task.get('task',{}).get('recognition_selection'))
        if mid in choices and choices[mid]['selection']!=selection:
            raise ValueError('同任务类别快照不一致：'+mid)
        choices[mid]=dict(selection=selection,source=str(path.relative_to(ROOT)))
    results=[]
    for session,mid,rows in missions:
        work=output/'isolated_input'/session.name
        paths=[p for p,_ in rows]+[p.with_suffix('.jpg') for p,_ in rows if p.with_suffix('.jpg').is_file()]
        reference=session/'time_references.json'
        if reference.is_file():paths.append(reference)
        for path in paths:
            originals[str(path)]=digest(path)
            dst=work/path.relative_to(session);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dst)
        for name in ('target-submission.json','raw-targets.json','dedup-decisions.json','submission-format.json'):
            old=session/'subject1_submissions'/mid/name
            if old.is_file():originals[str(old)]=digest(old)
        distance_count=sum(type(d.get('horizontal_distance_m')) in (int,float)
                           and math.isfinite(d['horizontal_distance_m']) and d['horizontal_distance_m']>=0 for _,d in rows)
        flown=choices.get(mid,dict(selection=optional_recognition_selection(None),source='missing; no inferred selection'))
        non_people=[cid for cid in flown['selection']['category_ids'] if cid<11]
        variants=[('as_flown',flown['selection']),
                  ('test_person_2',optional_recognition_selection(dict(category_ids=non_people+[12,16]))),
                  ('test_person_1_3',optional_recognition_selection(dict(category_ids=non_people+[11,13,15,17])))]
        for variant,selection in variants:
            save_reporting_selection(work,mid,selection)
            reporter=report.Subject1Reporter(work,publisher_dedup=True)
            built=reporter.build(mid,report.TEAM_NAME)
            prepared=reporter.prepare(built,already_deduplicated=True)
            frozen=json.loads(reporter.draft(prepared['draft_id']).read_text(encoding='utf8'))
            assert built['features']==frozen['features']
            package=work/'subject1_submissions'/mid
            audit=json.loads((package/'dedup-decisions.json').read_text(encoding='utf8'))
            format_audit=json.loads((package/'submission-format.json').read_text(encoding='utf8'))
            frozen_audit=json.loads((reporter.root/(prepared['draft_id']+'.format.json')).read_text(encoding='utf8'))
            assert frozen_audit['deduplication']['person_selection']==audit['person_selection']
            for key in ('static_quality_order','moving_quality_order','representative_quality_order'):
                assert frozen_audit['deduplication'][key] == audit[key]
            check_ranking(frozen,audit,json.loads((package/'raw-targets.json').read_text(encoding='utf8')))
            summary=report.validate_document(frozen)
            assert all(f['id']=='target-%03d'%i for i,f in enumerate(frozen['features'],1))
            assert all(not any(k.startswith('_') for k in f) for f in frozen['features'])
            for feature in frozen['features']:
                props=feature['properties']; model=props['targetModel']
                if props['targetType']=='人员':
                    assert re.fullmatch(r'人员[1-4]',model)
                    wanted=[cid-14 for cid in selection['category_ids'] if cid>=15] if props['targetCategory']=='移动' else [cid-10 for cid in selection['category_ids'] if 11<=cid<=14]
                    if wanted:assert int(model[-1]) in wanted
                if props.get('imagePath'):assert (package/props['imagePath']).is_file()
                if props['targetCategory']=='移动':assert len(props['trackPoints'])<=40
            submitted=check_submit(reporter,prepared,frozen)
            destination=output/'results'/session.name/variant
            for name in ('target-submission.json','raw-targets.json','dedup-decisions.json','submission-format.json'):
                save(destination/name,json.loads((package/name).read_text(encoding='utf8')))
            save(destination/'simulated-upload.json',submitted)
            entry=dict(session=session.name,mission_id=mid,variant=variant,raw_records=len(rows),
                raw_records_with_valid_distance=distance_count,selected_categories=selection,
                selection_source=flown['source'] if variant=='as_flown' else 'offline what-if; not the historical task selection',
                grouped_candidates=audit['raw_count'],deduplicated_candidates=audit['deduplicated_count'],
                **summary,person_remappings=len(audit['person_selection']['remappings']),
                converted_to_static=len(audit['motion_reclassifications']),
                nearest_observations=sum(a['rule']=='minimum_horizontal_distance' for a in format_audit['static_position_selection']),
                submitted_models=dict(Counter(f['properties']['targetCategory']+'/'+f['properties']['targetModel'] for f in frozen['features'])),
                ranking_verified=True,simulated_upload_verified=True,report=str(destination.relative_to(output)))
            results.append(entry)
            print(session.name,variant,summary,'remapped',entry['person_remappings'])
    assert all(digest(Path(p))==h for p,h in originals.items()),'历史原始数据发生变化'
    summary=dict(offline_only=True,no_real_http_requests=True,since=args.since,
        source_missions=len(missions),source_observations=sum(len(rows) for _,_,rows in missions),
        preserved_source_files=len(originals),source_file_sha256=originals,runs=results,
        limitations=['HTTP仅验证本程序协议并使用模拟回执，不代表赛方实际受理',
                     '历史缺少horizontal_distance_m时按原最近接收记录兼容；最近距离逻辑由独立构造样本测试'])
    save(output/'summary.json',summary)
    print('Saved',output/'summary.json')


if __name__=='__main__':main()
