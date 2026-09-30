import copy
import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.adapter import RecordingAdapter
from competition_backend.models import MissionPhase
from competition_backend.orchestrator import CompetitionOrchestrator, MissionError
from competition_shared.recognition import CATEGORY_CATALOG, optional_recognition_selection, validate_recognition_selection
from su17_competition_executor.task_protocol import TaskValidationError, assignment_checksum, validate_assignment
from test_orchestrator import make_config

SELECTION = {'category_count': 3, 'category_ids': [15, 0, 7]}


class RecognitionTest(unittest.TestCase):
    def test_catalog_and_optional_selection(self):
        self.assertEqual([item['id'] for item in CATEGORY_CATALOG], list(range(19)))
        self.assertEqual([item['name'] for item in CATEGORY_CATALOG],
                         ['车辆%d' % i for i in range(1, 8)] + ['工事%d' % i for i in range(1, 5)]
                         + ['人员%d' % i for i in range(1, 5)] + ['运动的人员%d' % i for i in range(1, 5)])
        normalized = validate_recognition_selection(SELECTION)
        self.assertEqual(normalized['category_ids'], [0, 7, 15])
        self.assertEqual(normalized['category_names'], ['车辆1', '工事1', '运动的人员1'])
        self.assertEqual(optional_recognition_selection(None)['category_ids'], [])
        self.assertEqual(validate_recognition_selection({'category_count': 0, 'category_ids': []})['category_count'], 0)
        self.assertEqual(optional_recognition_selection({'category_count': 99, 'category_ids': [18, 18, -1, True, 0]})['category_ids'], [0, 18])
        for bad in [None, {}, {'category_count': 2, 'category_ids': [1, 1]},
                    {'category_count': 1, 'category_ids': [19]}, {'category_count': True, 'category_ids': [1]},
                    {'category_count': 1, 'category_ids': [True]}, {'category_count': 1, 'category_ids': [1.0]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_recognition_selection(bad)

    def test_api_defaults_to_zero_and_does_not_restore_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, 'recognition_selection.json'), 'w', encoding='utf-8') as stream:
                stream.write('{"category_count": 1, "category_ids": [4]}')
            env = dict(os.environ, COMPETITION_ADAPTER='sim', COMPETITION_DATA_DIR=directory)
            client = TestClient(create_app(env))
            response = client.get('/api/v1/recognition-categories')
            self.assertEqual(response.json()['selection']['category_ids'], [])
            self.assertEqual(len(response.json()['catalog']), 19)
            self.assertEqual(client.put('/api/v1/recognition-categories', json=SELECTION).status_code, 405)
            for payload, expected in [({'subject': 'subject1', 'require_recognition_selection': True}, []),
                                      ({'subject': 'subject1', 'recognition_selection': SELECTION}, [0, 7, 15]),
                                      ({'subject': 'subject1', 'recognition_selection': {'category_ids': 'bad'}}, [])]:
                plan = client.post('/api/v1/plan', json=payload)
                self.assertEqual(plan.status_code, 200, plan.text)
                for task in plan.json()['mission']['planned_uavs'].values():
                    self.assertEqual(task['task']['recognition_selection']['category_ids'], expected)
            self.assertEqual(TestClient(create_app(env)).get('/api/v1/recognition-categories').json()['selection']['category_ids'], [])
            plan = client.post('/api/v1/plan', json={'subject': 'subject2'})
            self.assertEqual(plan.status_code, 200, plan.text)
            self.assertTrue(all('recognition_selection' not in task['task'] for task in plan.json()['mission']['planned_uavs'].values()))

    def test_assignment_checksum_and_running_task_protection(self):
        adapter = RecordingAdapter()
        backend = CompetitionOrchestrator(make_config(), adapter)
        tasks = {i: {'type': 'lawnmower_search', 'coordinate_frame': 'ENU', 'waypoints_m': [[0, 0], [1, 0]]} for i in range(1, 7)}
        before = copy.deepcopy(tasks)
        backend.plan('subject1', tasks_by_uav=tasks, recognition_selection=SELECTION)
        self.assertEqual(tasks, before)
        self.assertEqual(len(adapter.commands), 6)
        for command in adapter.commands:
            message = dict(command['payload'], type='assign_task', uav_id=command['uav_id'])
            self.assertEqual(validate_assignment(message, command['uav_id'])['task']['recognition_selection']['category_ids'], [0, 7, 15])
            corrupted = copy.deepcopy(message)
            corrupted['task']['recognition_selection']['category_ids'] = [0, 7, 18]
            with self.assertRaises(TaskValidationError):
                validate_assignment(corrupted, command['uav_id'])
            corrupted['assignment_checksum'] = assignment_checksum(corrupted)
            self.assertEqual(validate_assignment(corrupted, command['uav_id'])['task']['recognition_selection']['category_ids'], [0, 7, 18])
        backend._mission.phase = MissionPhase.RUNNING
        with self.assertRaises(MissionError):
            backend.plan('subject1', tasks_by_uav=tasks, recognition_selection={'category_count': 0, 'category_ids': []})
        self.assertEqual(len(adapter.commands), 6)


if __name__ == '__main__':
    unittest.main()
