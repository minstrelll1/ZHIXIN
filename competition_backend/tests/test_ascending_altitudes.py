"""新增高度选项经固定规划 API 分派后，六机扫描和返航高度保持一致。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src/su17_competition_executor/test'))
import test_executor_autonomy as autonomy
from competition_backend.api import create_app
from competition_backend.final_scenes import PROFILES as FINAL_PROFILES, departure_for
from competition_backend.transit_routes import PROFILES
from su17_competition_executor.task_protocol import validate_assignment
from su17_competition_executor.transit_protocol import route_messages


class AscendingAltitudesTest(unittest.TestCase):
    def test_legacy_scenes_dispatch_and_publish_new_heights_with_old_config(self):
        heights = [34., 37., 40., 47., 50., 53.]
        with tempfile.TemporaryDirectory() as directory:
            app = create_app({'COMPETITION_ADAPTER':'sim','COMPETITION_DATA_DIR':directory,
                              'COMPETITION_DIAGNOSTICS_DIR':directory})
            try:
                # 已部署的旧配置可能没有这个键，仍须能下发指定高度。
                for template in app.state.orchestrator.config.subjects.values():
                    template['takeoff_altitudes_m_by_profile'].pop('ascending34m',None)
                client = TestClient(app)
                for profile in PROFILES:
                    departure = ('fixed_dalian' if profile=='dalian_nanshan' else 'fixed_xuchang' if profile=='xuchang_small'
                                 else 'stadium_center' if profile in ('subject1_actual','subject2_actual') else 'southeast')
                    with self.subTest(profile=profile):
                        app.state.adapter.commands.clear()
                        response = client.post('/api/v1/plan',json=dict(
                            subject='subject2' if profile=='subject2_actual' else 'subject1', planning_mode='competition',
                            controller_mode='external',flight_profile=profile,flight_altitude_plan='ascending34m',
                            coordinate_mode='gps',departure_point=departure,
                            gps_origin={'latitude':30.78528,'longitude':103.86102,'altitude_m':44.098}))
                        self.assertEqual(response.status_code,200,response.text)
                        commands=[c for c in app.state.adapter.snapshot() if c['type']=='assign_task']
                        self.assertEqual(len(commands),6)
                        for command in commands:
                            uid=command['uav_id'];height=heights[uid-1]
                            assignment=validate_assignment(dict(command['payload'],type='assign_task',uav_id=uid),uid)
                            self.assertEqual(assignment['target_altitude_m'],height)
                            entry,returned=route_messages(assignment)
                            self.assertTrue(all(p[2]==height for p in entry['path']))
                            self.assertTrue(all(p[2]==height for r in returned['routes'] for p in r['path']))
                            node=autonomy.AutonomyTest().executor(directory)
                            node.uav_id=uid;node._assignment=assignment
                            node.external_entry_path_pub=Mock();node.external_return_paths_pub=Mock()
                            node._publish_external_mission({'deadline_at':None})
                            self.assertEqual(node.external_path_pub.publish.call_args.args[0].data[2::3],
                                             [height]*len(assignment['task']['waypoints_m']))
                            self.assertEqual(node.external_landing_pub.publish.call_args.args[0].data[2],height)
                            manifest=json.loads(node.external_mission_pub.publish.call_args.args[0].data)
                            self.assertEqual(manifest['relative_altitude_m'],height)
                before=len(app.state.adapter.snapshot())
                for profile in FINAL_PROFILES:
                    response=client.post('/api/v1/plan',json=dict(subject='subject2',planning_mode='competition',
                        controller_mode='external',flight_profile=profile,flight_altitude_plan='ascending34m',
                        coordinate_mode='gps',departure_point=departure_for(profile)))
                    self.assertEqual(response.status_code,422,response.text)
                self.assertEqual(len(app.state.adapter.snapshot()),before)
            finally:
                app.state.audit.close()


if __name__=='__main__':unittest.main()
