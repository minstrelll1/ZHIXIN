"""把离线端到端测试捕获的消息整理为程序 B 可审阅的完整示例。"""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(source, destination):
    evidence = json.loads(Path(source).read_text(encoding='utf-8'))
    if not evidence.get('offline_only') or not evidence.get('simulated_homes'):
        raise ValueError('只接收明确标记的离线模拟证据')
    prepared = json.loads((ROOT/'competition_backend/competition_backend/final_scenes_prepared.json').read_text(encoding='utf-8'))
    destination = Path(destination)
    index = dict(offline_only=True, simulated_homes=True,
                 source_plan_sha256=prepared['source_plan_sha256'], fixed_bundle_sha256=prepared['sha256'],
                 description='来自实际代码的离线 API→六地面端→机载缓存→起飞成功发布。所有起降点为模拟值，不用于实飞。',
                 scenes=[], files={})
    def save(name, value):
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
        path.write_bytes(raw)
        index['files'][name] = hashlib.sha256(raw).hexdigest()
    def message(topic, ros_type, value):
        result = dict(topic=topic, message_type='std_msgs/'+ros_type)
        if ros_type == 'String':
            result['message'] = {'data':json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)}
        elif ros_type.endswith('MultiArray'):
            result['message'] = {'layout':{'dim':[], 'data_offset':0}, 'data':value}
        else:
            result['message'] = {'data':value}
        return result
    for scene in evidence['scenes']:
        sid = scene['scene_id']
        assert len(scene['aircraft']) == 6
        scene_index = dict(scene_id=sid, dispatch_seconds=scene['dispatch_seconds'],
                           planning_preview=sid+'/planning_preview.json', aircraft=[])
        save(scene_index['planning_preview'], dict(offline_only=True, simulated_homes=True, mission=scene['plan']))
        for aircraft in scene['aircraft']:
            uid = aircraft['uav_id']; prefix='/ground_mission_planner/vehicle_%d/'%uid; comp='/uav%d/competition/'%uid
            assignment = aircraft['assignment']
            if assignment['task']['source_plan_sha256'] != prepared['source_plan_sha256']:
                raise ValueError('示例与当前固定航点来源不一致')
            assert assignment['task']['waypoints_m'] == prepared['scenes'][sid]['plan']['planned_uavs'][str(uid)]['task']['waypoints_m']
            fixed_uav = prepared['scenes'][sid]['plan']['planned_uavs'][str(uid)]
            if (assignment['target_altitude_m'] != fixed_uav['target_altitude_m']
                    or aircraft['takeoff']['target_altitude_m'] != fixed_uav['target_altitude_m']
                    or assignment['task'].get('layers') != fixed_uav['task'].get('layers')):
                raise ValueError('示例层高或分层航点与当前固定方案不一致，请重跑离线分派测试')
            row = dict(offline_only=True, simulated_homes=True, scene_id=sid, uav_id=uid,
                       note='空 fixed_targets 与空 recognition_categories 是本次离线测试输入，现场按任务发布端配置下发。',
                       plan_and_assign_to_onboard=assignment, one_click_takeoff_to_onboard=aircraft['takeoff'],
                       ros_after_assignment=[message(comp+key,kind,aircraft[key]) for key,kind in
                           [('task_region','String'),('fixed_targets','String'),('recognition_categories','Int32MultiArray'),('competition_time','String')]],
                       ros_after_successful_takeoff=[message(prefix+key,kind,aircraft[key]) for key,kind in
                           [('entry_path','String'),('return_paths','String'),('path_stage_1','Float64MultiArray'),
                            ('path_stage_2','Float64MultiArray'),('jiangluodian','Float64MultiArray')]]+
                           [message(comp+'external_mission','String',aircraft['external_mission']),
                            message('/ground_mission_planner/recon_start_mode','Int32',0)],
                       manual_return_only_example=message(prefix+'return_home','Bool',True))
            filename=sid+'/uav%d.json'%uid
            save(filename,row)
            scene_index['aircraft'].append(dict(uav_id=uid, file=filename,
                stage_1_count=len(aircraft['path_stage_1'])//3, stage_2_count=len(aircraft['path_stage_2'])//3,
                task_altitude_m=assignment['target_altitude_m'],
                return_route_count=len(aircraft['return_paths']['routes'])))
        index['scenes'].append(scene_index)
    save('index.json',index)
    with zipfile.ZipFile(destination.with_suffix('.zip'), 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(destination.rglob('*.json')):
            archive.write(path, path.relative_to(destination).as_posix())
        archive.write(ROOT/'docs/final_scenes_integration.txt','README.txt')
    print('Saved',destination,'(18 aircraft examples, 3 importable plans)')


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('evidence')
    parser.add_argument('--output',default=str(ROOT/'docs/final_scenes_program_b_examples'))
    args=parser.parse_args()
    build(args.evidence,args.output)
