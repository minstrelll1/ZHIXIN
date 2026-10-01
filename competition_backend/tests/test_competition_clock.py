"""比赛计时与跨六地面端同步；只使用内存 HTTP，不连接飞行器。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from competition_backend.api import create_app
from competition_backend.competition_clock import CompetitionClock
from competition_shared.fleet import default_fleet, apply_fixed_binding


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.
        self.clock = CompetitionClock(lambda:self.now)

    def test_starts_once_and_edit_continues_from_entered_elapsed(self):
        self.assertFalse(self.clock.snapshot()['running'])
        state=self.clock.start(1, 5.)
        self.now+=10
        self.assertEqual(self.clock.start(1)['session_id'],state['session_id'])
        self.assertEqual(self.clock.snapshot()['elapsed_seconds'],15)
        self.clock.edit(87,3,state['session_id'],'r1')
        self.now+=5
        self.assertEqual(self.clock.snapshot()['elapsed_seconds'],92)
        self.assertEqual(self.clock.snapshot()['updated_by'],3)
        self.clock.edit(87,3,state['session_id'],'r1')
        self.assertEqual(self.clock.snapshot()['elapsed_seconds'],92)
        self.assertEqual(self.clock.snapshot()['revision'],1)

    def test_edits_ordered_and_stale_snapshots_cannot_roll_back(self):
        state=self.clock.start(1)
        early=self.clock.edit(50,2,state['session_id'],'a')
        late=self.clock.edit(20,3,state['session_id'],'b')
        follower=CompetitionClock(lambda:2000.)
        self.assertTrue(follower.accept(late,1))
        self.assertFalse(follower.accept(early,1))
        self.assertEqual(follower.snapshot()['elapsed_seconds'],20)
        self.assertFalse(follower.accept(late,2))

    def test_disconnection_keeps_counting_and_new_session_rejects_old_session(self):
        state=self.clock.start(1)
        follower=CompetitionClock(lambda:self.now+10000)
        follower.accept(state,1,.2)
        self.now+=10
        self.assertAlmostEqual(follower.snapshot()['elapsed_seconds'],10.1)
        self.assertFalse(follower.snapshot()['synchronized'])
        self.clock.reset()
        new=self.clock.start(1)
        self.assertTrue(follower.accept(new,1))
        self.assertFalse(follower.accept(state,1))
        with self.assertRaisesRegex(ValueError,'会话已变化'):
            self.clock.edit(5,2,state['session_id'],'old-session')

    def test_invalid_values_and_duplicate_request_conflict(self):
        session=self.clock.start(1)['session_id']
        for value in (-1, True, '30', float('nan'), float('inf'), 400*86400):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.clock.edit(value,2,session,'a')
        self.clock.edit(1,2,session,'a')
        with self.assertRaises(ValueError):
            self.clock.edit(2,2,session,'a')


class SixGroundClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.apps,self.clients={},{}
        self.now=1000.
        fleet=default_fleet()
        for uid in range(1,7):
            fleet=apply_fixed_binding(fleet,uid,'p600')
        for uid in range(1,7):
            root=Path(self.tmp.name)/str(uid)
            root.mkdir()
            config=root/'fleet.json'
            config.write_text(json.dumps(fleet),encoding='utf-8')
            app=create_app({'COMPETITION_FLEET_CONFIG':str(config), 'COMPETITION_ADAPTER':'distributed',
                'COMPETITION_GROUND_TERMINAL_ID':str(uid),'COMPETITION_TASK_PUBLISHER':'',
                'COMPETITION_PEER_TOKEN':'test-secret','COMPETITION_DATA_DIR':str(root),
                'COMPETITION_DIAGNOSTICS_DIR':str(root/'logs')})
            self.addCleanup(app.state.audit.close)
            app.state.competition_clock._now=lambda uid=uid:self.now+uid*10000
            app.state.adapter._request_json=self.network
            app.state.adapter.local_adapter.forward_command=Mock()
            self.apps[uid]=app
            self.clients[uid]=TestClient(app)
        self.headers={'X-Competition-Peer-Token':'test-secret'}

    def network(self,url,method='GET',payload=None):
        for uid,client in self.clients.items():
            base='http://192.168.2.%d:8000'%(197+5*uid)
            if url.startswith(base+'/'):
                response=client.request(method,url[len(base):],json=payload,headers=self.headers)
                if response.status_code>=400:
                    raise RuntimeError(response.json().get('detail'))
                return response.json()
        raise RuntimeError('离线地面端')

    def select(self,uid,publisher=False):
        response=self.clients[uid].post('/api/v1/operator',json={'ground_terminal_id':uid,'model':'p600','task_publisher':publisher})
        self.assertEqual(response.status_code,200,response.text)

    def poll(self):
        for uid in range(2,7):
            adapter=self.apps[uid].state.adapter
            adapter._poll_peer_once(1,adapter.peers[1])

    def test_all_six_can_edit_and_synchronize_without_drones_or_missions(self):
        for uid in range(2,7):
            self.select(uid)
            self.assertFalse(self.apps[uid].state.competition_clock.snapshot()['running'])
        self.select(1,True)
        session=self.apps[1].state.competition_clock.snapshot()['session_id']
        self.now+=20
        self.poll()
        for uid in range(1,7):
            response=self.clients[uid].put('/api/v1/competition-clock',json={
                'elapsed_seconds':uid*60,'session_id':session,'request_id':'edit-'+str(uid)})
            self.assertEqual(response.status_code,200,response.text)
            self.poll()
            for target in range(1,7):
                state=self.clients[target].get('/api/v1/status').json()['competition_clock']
                self.assertEqual(state['revision'],uid)
                self.assertEqual(state['updated_by'],uid)
                self.assertAlmostEqual(state['elapsed_seconds'],uid*60,delta=.2)
                self.apps[target].state.adapter.local_adapter.forward_command.assert_not_called()
        self.now+=10
        self.poll()
        self.assertAlmostEqual(self.apps[6].state.competition_clock.snapshot()['elapsed_seconds'],370,delta=.2)
        self.select(1,True)
        self.assertEqual(self.apps[1].state.competition_clock.snapshot()['session_id'],session)

    def test_authentication_stale_session_and_unavailable_publisher(self):
        self.select(2)
        self.select(1,True)
        self.poll()
        session=self.apps[1].state.competition_clock.snapshot()['session_id']
        data={'elapsed_seconds':60,'session_id':session,'request_id':'a','edited_by':2}
        self.assertEqual(self.clients[1].put('/api/v1/peer/competition-clock',json=data).status_code,403)
        self.assertEqual(self.clients[2].put('/api/v1/peer/competition-clock',json=data,headers=self.headers).status_code,409)
        self.assertEqual(self.clients[1].put('/api/v1/competition-clock',json={**data,'session_id':'old'}).status_code,409)
        self.apps[2].state.adapter._request_json=Mock(side_effect=TimeoutError('离线'))
        before=self.apps[2].state.competition_clock.snapshot()
        self.assertEqual(self.clients[2].put('/api/v1/competition-clock',json=data).status_code,503)
        after=self.apps[2].state.competition_clock.snapshot()
        self.assertEqual(after['revision'],before['revision'])


if __name__=='__main__':
    unittest.main()
