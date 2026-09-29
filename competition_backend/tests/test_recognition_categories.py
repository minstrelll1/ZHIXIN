import copy
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.recognition_settings import RecognitionSettings
from competition_backend.adapter import RecordingAdapter
from competition_backend.orchestrator import CompetitionOrchestrator, MissionError
from competition_backend.models import MissionPhase
from competition_shared.recognition import CATEGORY_CATALOG, validate_recognition_selection
from su17_competition_executor.task_protocol import validate_assignment, assignment_checksum, TaskValidationError
from test_orchestrator import make_config

SELECTION = {'category_count': 3, 'category_ids': [12, 1, 8]}

class RecognitionTest(unittest.TestCase):
    def test_catalog_validation_and_atomic_save(self):
        self.assertEqual([v['id'] for v in CATEGORY_CATALOG], list(range(1,16)))
        normalized=validate_recognition_selection(SELECTION)
        self.assertEqual(normalized['category_names'], ['车辆1','工事1','人员1'])
        self.assertEqual(normalized['category_ids'], [1,8,12])
        with tempfile.TemporaryDirectory() as directory:
            settings=RecognitionSettings(directory)
            self.assertIsNone(settings.read())
            settings.save(SELECTION)
            for bad in [None, {}, {'category_count':0,'category_ids':[]},
                        {'category_count':2,'category_ids':[1,1]},
                        {'category_count':1,'category_ids':[16]},
                        {'category_count':True,'category_ids':[1]},
                        {'category_count':1,'category_ids':[True]},
                        {'category_count':1,'category_ids':[1.0]},
                        {'category_count':3,'category_ids':[1,8]}]:
                with self.subTest(bad=bad), self.assertRaises(ValueError): settings.save(bad)
            self.assertEqual(RecognitionSettings(directory).read(),normalized)

    def test_api_save_plan_and_subject_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            env=dict(os.environ,COMPETITION_ADAPTER='sim',COMPETITION_DATA_DIR=directory)
            app=create_app(env);client=TestClient(app)
            self.assertIsNone(client.get('/api/v1/recognition-categories').json()['selection'])
            self.assertEqual(client.post('/api/v1/plan',json={'subject':'subject1','require_recognition_selection':True}).status_code,422)
            r=client.put('/api/v1/recognition-categories',json=SELECTION)
            self.assertEqual(r.status_code,200,r.text)
            self.assertEqual(client.get('/api/v1/status').json()['mission'],None)
            self.assertEqual(TestClient(create_app(env)).get('/api/v1/recognition-categories').json()['selection'],r.json()['selection'])
            for subject in ['subject1','subject2']:
                plan=client.post('/api/v1/plan',json={'subject':subject})
                self.assertEqual(plan.status_code,200,plan.text)
                for task in plan.json()['mission']['planned_uavs'].values():
                    self.assertEqual('recognition_selection' in task['task'],subject=='subject1')
            bad=client.put('/api/v1/recognition-categories',json={'category_count':1,'category_ids':[0]})
            self.assertEqual(bad.status_code,422)
            self.assertEqual(client.get('/api/v1/recognition-categories').json()['selection'],r.json()['selection'])

    def test_all_assignments_checksummed_and_started_task_immutable(self):
        config=make_config();adapter=RecordingAdapter()
        backend=CompetitionOrchestrator(config,adapter)
        tasks={i:{'type':'lawnmower_search','coordinate_frame':'ENU','waypoints_m':[[0,0],[1,0]]} for i in range(1,7)}
        before=copy.deepcopy(tasks)
        backend.plan('subject1',tasks_by_uav=tasks,recognition_selection=SELECTION)
        self.assertEqual(tasks,before)
        self.assertEqual(len(adapter.commands),6)
        for cmd in adapter.commands:
            msg=dict(cmd['payload'],type='assign_task',uav_id=cmd['uav_id'])
            self.assertEqual(validate_assignment(msg,cmd['uav_id'])['task']['recognition_selection']['category_ids'],[1,8,12])
            corrupted=copy.deepcopy(msg);corrupted['task']['recognition_selection']['category_ids']=[1,8,13]
            self.assertNotEqual(assignment_checksum(corrupted),msg['assignment_checksum'])
            with self.assertRaises(TaskValidationError):validate_assignment(corrupted,cmd['uav_id'])
            corrupted['task']['recognition_selection']['category_ids']=[1,1,8]
            corrupted['assignment_checksum']=assignment_checksum(corrupted)
            with self.assertRaises(TaskValidationError):validate_assignment(corrupted,cmd['uav_id'])
        backend._mission.phase=MissionPhase.RUNNING
        with self.assertRaises(MissionError):backend.plan('subject1',tasks_by_uav=tasks,recognition_selection={'category_count':1,'category_ids':[2]})
        self.assertEqual(len(adapter.commands),6)

if __name__=='__main__':unittest.main()
