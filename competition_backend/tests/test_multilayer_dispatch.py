"""真实任务序列化、缓存和 ROS 发布方法的离线双层接口回归，不连接飞机。"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
for relative in ('src/su17_competition_executor/test', 'src/su17_competition_executor/src'):
    sys.path.insert(0, str(ROOT / relative))
import test_executor_autonomy as autonomy
from test_orchestrator import make_config
from competition_backend.adapter import RecordingAdapter
from competition_backend.orchestrator import CompetitionOrchestrator, MissionError
from su17_competition_executor.task_protocol import (
    assignment_checksum, validate_assignment, TaskValidationError, save_assignment_atomic, load_assignment,
)


def example_plan(frame='WGS84'):
    """示例坐标只用于接口测试；六机不同落点，上下层点数也不同。"""
    config = make_config()
    config.subjects['subject2'] = copy.deepcopy(config.subjects['subject1'])
    config.takeoff_altitudes_m = {uid: float([47, 50, 53][(uid - 1) % 3]) for uid in range(1, 7)}
    tasks, catalog = {}, {}
    for uid in range(1, 7):
        def gps(p):
            return [33.863 + p[0] / 111111., 113.706 - p[1] / 92000.]
        upper = [[uid * 10., 20.], [uid * 10. + 5., 30.]]
        lower = [[uid * 10. + 5., 28.], [uid * 10. + 2., 22.], [uid * 10., 15.]]
        layers = [dict(layer_id=label, altitude_m=config.takeoff_altitudes_m[uid] - drop,
                       waypoints_m=points, waypoints_wgs84=[gps(p) for p in points],
                       hover_scan_seconds=50., scan_passes=1)
                  for label, drop, points in [('upper', 0, upper), ('lower', 13, lower)]]
        tasks[uid] = dict(type='lawnmower_search', coordinate_frame='LOCAL_NORTH_WEST' if frame == 'WGS84' else 'ENU',
                          waypoints_m=upper, waypoints_wgs84=[gps(p) for p in upper],
                          reconnaissance_mode='hover_scan', hover_scan_seconds=50.,
                          execution_mode='upper_once_then_lower_once', layer_order=['upper', 'lower'], layers=layers)
        catalog[str(uid)] = [[p[1], p[0]] for p in tasks[uid]['waypoints_wgs84']] if frame == 'WGS84' else upper
    for uid, task in tasks.items():
        home = [113.7056 + uid / 100000., 33.8644] if frame == 'WGS84' else [float(uid), 0.]
        task['transit_routes'] = dict(
            schema_version=2, recipient_uav_id=uid, route_scope='all_uav_waypoints',
            plan_sha256='offline-example-not-a-flight-plan', coordinate_frame=frame,
            departure=home, entry_path=[home, catalog[str(uid)][0]], waypoints_by_uav=copy.deepcopy(catalog),
            return_paths=[dict(source_uav_id=int(source), waypoint_index=i + 1, path=[point, home])
                          for source, points in catalog.items() for i, point in enumerate(points)])
    return config, dict(search_area=dict(coordinate_mode='gps' if frame == 'WGS84' else 'xyz'),
                        planned_uavs={str(uid): {'task': task} for uid, task in tasks.items()})


class MultilayerDispatchTest(unittest.TestCase):
    def dispatch(self, frame='WGS84'):
        config, plan = example_plan(frame)
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(config, adapter)
        backend.plan('subject2', prepared_plan=plan, controller_mode='external')
        return [dict(command['payload'], type=command['type'], uav_id=command['uav_id'])
                for command in adapter.snapshot()]

    def node(self, assignment, directory):
        node = autonomy.AutonomyTest().executor(directory)
        node.uav_id = assignment['uav_id']
        node._assignment = assignment
        node.external_path_topic = '/ground_mission_planner/vehicle_{}/path_stage_1'.format(node.uav_id)
        node.external_lower_path_topic = '/ground_mission_planner/vehicle_{}/path_stage_2'.format(node.uav_id)
        node.external_entry_path_pub, node.external_return_paths_pub = Mock(), Mock()
        node._checkpoint = Mock()
        return node

    def test_six_uav_ground_checksum_cache_takeoff_and_program_b_messages(self):
        for frame in ('WGS84', 'ENU'):
            with self.subTest(frame=frame), tempfile.TemporaryDirectory() as directory:
                commands = self.dispatch(frame)
                self.assertEqual(len(commands), 6)
                for command in commands:
                    checked = validate_assignment(json.loads(json.dumps(command)), command['uav_id'])
                    save_assignment_atomic(str(Path(directory) / 'task.json'), checked)
                    restored = load_assignment(str(Path(directory) / 'task.json'))
                    node = self.node(restored, directory)
                    order = []
                    for name in ('entry_path', 'return_paths', 'path', 'lower_path', 'landing', 'mission'):
                        getattr(node, 'external_' + name + '_pub').publish.side_effect = lambda _, name=name: order.append(name)
                    node._publish_recon_start_mode = Mock(side_effect=lambda _: order.append('start'))
                    node._motion_entry('takeoff', lambda _: True, {'mission_id': checked['mission_id']})
                    self.assertEqual(order, ['entry_path', 'return_paths', 'path', 'lower_path', 'landing', 'mission', 'start'])
                    upper, lower = checked['task']['layers']
                    for layer, pub in ((upper, node.external_path_pub), (lower, node.external_lower_path_pub)):
                        points = layer['waypoints_wgs84'] if frame == 'WGS84' else layer['waypoints_m']
                        expected = [v for p in points for v in ((p[1], p[0], layer['altitude_m']) if frame == 'WGS84'
                                                               else (p[0], p[1], layer['altitude_m']))]
                        self.assertEqual(pub.publish.call_args.args[0].data, expected)
                    returned = json.loads(node.external_return_paths_pub.publish.call_args.args[0].data)
                    self.assertEqual(returned['waypoint_layer'], 'upper')
                    self.assertEqual(returned['return_policy'], 'climb_to_upper_then_return')
                    self.assertEqual(len(returned['routes']), 12)  # 六机上层各 2 点，不包含下层各 3 点
                    self.assertEqual(returned['waypoint_counts_by_uav'], {str(uid): 2 for uid in range(1, 7)})
                    home = node.external_landing_pub.publish.call_args.args[0].data
                    for route in returned['routes']:
                        self.assertEqual(route['path'][-1], home)
                        self.assertTrue(all(p[2] == upper['altitude_m'] for p in route['path']))
                    manifest = json.loads(node.external_mission_pub.publish.call_args.args[0].data)
                    self.assertEqual(manifest['layer_order'], ['upper', 'lower'])
                    self.assertEqual(manifest['layers'][1]['waypoint_count'], 3)
                    self.assertEqual(manifest['assignment_checksum'], checked['assignment_checksum'])
                    node._fly_to.assert_not_called()  # 接口发布不执行换层或返航动作

    def test_failed_takeoff_does_not_publish_either_layer_or_start(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(self.dispatch()[0], directory)
            node._motion_entry('takeoff', lambda _: False, {'mission_id': node._assignment['mission_id']})
            for pub in (node.external_path_pub, node.external_lower_path_pub, node.external_return_paths_pub,
                        node.external_mission_pub, node.recon_start_mode_pub):
                pub.publish.assert_not_called()

    def test_invalid_last_aircraft_rejected_before_any_dispatch(self):
        mutations = [
            lambda t: t['layers'][1].update(altitude_m=100),
            lambda t: t['layers'][1].update(waypoints_wgs84=[[float('nan'), 113.7]]),
            lambda t: t['layers'][1].update(scan_passes=2),
            lambda t: t['transit_routes']['waypoints_by_uav']['1'].append([113.7, 33.86]),
            lambda t: t.update(waypoints_m=t['layers'][1]['waypoints_m']),
        ]
        for mutate in mutations:
            config, plan = example_plan()
            mutate(plan['planned_uavs']['6']['task'])
            adapter = RecordingAdapter()
            with self.assertRaises(MissionError):
                CompetitionOrchestrator(config, adapter).plan('subject2', prepared_plan=plan, controller_mode='external')
            self.assertEqual(adapter.snapshot(), [])

    def test_onboard_rejects_corruption_and_incorrect_layer_contract(self):
        message = self.dispatch()[0]
        changed = copy.deepcopy(message)
        changed['task']['layers'][1]['waypoints_m'][0][0] += 1
        with self.assertRaises(TaskValidationError):
            validate_assignment(changed, 1)  # 下层也在原任务 SHA-256 覆盖范围内
        mutations = [
            lambda a: a.update(subject='subject1'),
            lambda a: a.update(controller_mode='internal'),
            lambda a: a.update(target_altitude_m=48),
            lambda a: a['task'].update(layer_order=['lower', 'upper']),
            lambda a: a['task']['layers'][1].update(hover_scan_seconds=-1),
            lambda a: a['task']['layers'][1].update(waypoints_wgs84=[[33., 181.]] * 3),
            lambda a: a['task']['transit_routes']['return_paths'].append(
                dict(source_uav_id=1, waypoint_index=3, path=[[113.7, 33.86], [113.70561, 33.8644]])),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(message)
            mutate(changed)
            changed['assignment_checksum'] = assignment_checksum(changed)
            with self.assertRaises(TaskValidationError):
                validate_assignment(changed, 1)

    def test_legacy_subject2_and_subject1_clear_old_lower_without_changing_upper(self):
        with tempfile.TemporaryDirectory() as directory:
            node = self.node(self.dispatch()[0], directory)
            node._publish_external_mission({})
            original_upper = node.external_path_pub.publish.call_args.args[0].data
            for subject in ('subject2', 'subject1'):
                task = node._assignment['task']
                task.pop('layers', None)
                task.pop('layer_order', None)
                task.pop('execution_mode', None)
                task['scan_passes'] = 2
                node._assignment['subject'] = subject
                node._publish_external_mission({})
                self.assertEqual(node.external_path_pub.publish.call_args.args[0].data, original_upper)
                self.assertEqual(node.external_lower_path_pub.publish.call_args.args[0].data, [])
                manifest = json.loads(node.external_mission_pub.publish.call_args.args[0].data)
                if subject == 'subject2':
                    self.assertEqual(manifest['execution_mode'], 'legacy_single_layer')
                    self.assertEqual(manifest['layers'][0]['scan_passes'], 2)
                    self.assertEqual(manifest['layers'][1]['waypoints'], [])
                else:
                    self.assertNotIn('layers', manifest)
                    self.assertNotIn('waypoint_layer', json.loads(node.external_return_paths_pub.publish.call_args.args[0].data))


if __name__ == '__main__':
    unittest.main()
