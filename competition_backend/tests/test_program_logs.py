"""日志跨启动、断线补读、中文分页及网页旧游标回归；不启动机载业务程序。"""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from competition_backend.ground_entry import GroundEntry
from competition_backend.program_logs import LatestProgramLog
from competition_backend.program_manager import ProgramManager
from competition_shared.fleet import default_fleet
from test_program_manager import remote


def page(text, session='a', start=0, read_start=0):
    data = text.encode('utf-8') if isinstance(text, str) else text
    return dict(log_session=session, log_start=start, log_read_start=read_start,
                log=base64.b64encode(data).decode(), offset=read_start+len(data))


class LatestMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'uav1_flight.log'
        self.mirror = self.create()

    def create(self, host='uav1'):
        return LatestProgramLog(self.path, host, lambda text: text.replace('secret', '[隐藏]'))

    def text(self):
        return self.path.read_text(encoding='utf-8')

    def test_new_session_replaces_only_this_program_and_times_every_line(self):
        other=self.path.with_name('uav1_detection.log')
        other.write_text('其他程序日志',encoding='utf-8')
        self.path.write_text('旧版累积日志',encoding='utf-8')
        self.mirror.consume(page('第一行\n第二行\nsecret\n'))
        self.assertNotIn('旧版累积', self.text())
        for line in self.text().splitlines():
            self.assertRegex(line, r'^\[地面(?:接收|记录) \d{4}-\d\d-\d\d .+\+08:00\] ')
        self.assertNotIn('secret',self.text())
        self.mirror.consume(page('新一次程序\n',session='b',start=100,read_start=100))
        self.assertNotIn('第一行',self.text())
        self.assertIn('新一次程序',self.text())
        self.assertEqual(other.read_text(encoding='utf-8'),'其他程序日志')

    def test_reconnect_and_ground_restart_resume_without_duplicate_or_clear(self):
        first=page('中文开始\n')
        self.mirror.consume(first)
        old_generation=self.mirror.generation
        self.mirror.consume({})
        restarted=self.create()
        self.assertEqual(restarted.offset,first['offset'])
        self.assertEqual(restarted.generation,old_generation)
        restarted.consume(page('恢复后\n',read_start=restarted.offset))
        self.assertEqual(self.text().count('中文开始'),1)
        self.assertIn('恢复后',self.text())
        restarted.consume(first)  # 重复回包不得重复追加
        self.assertEqual(self.text().count('中文开始'),1)

    def test_partial_line_and_split_utf8_survive_restart(self):
        data='中文一行\n'.encode()
        self.mirror.consume(page(data[:4]))
        restarted=self.create()
        restarted.consume(page(data[4:],read_start=4))
        self.assertIn('中文一行',self.text())
        self.assertNotIn('\ufffd',self.text())
        self.assertEqual(self.text().count('[地面接收'),1)

    def test_stale_remote_offset_after_rotation_refetches_from_start(self):
        self.mirror.consume(page('前一次\n'))
        self.mirror.consume(page('中段不完整',session='b',read_start=15))
        self.assertEqual(self.mirror.offset,0)
        self.assertNotIn('中段不完整',self.text())
        self.mirror.consume(page('新的完整内容\n',session='b'))
        self.assertIn('新的完整内容',self.text())

    def test_missing_or_modified_copy_and_host_change_refetch(self):
        self.mirror.consume(page('记录\n'))
        self.assertEqual(self.create('other').offset,0)
        self.path.write_text('被修改',encoding='utf-8')
        self.assertEqual(self.create().offset,0)

    def test_web_old_generation_restarts_paging_even_when_new_file_is_larger(self):
        manager=ProgramManager(self.temp.name,{})
        manager.local=dict(uav_id=1,onboard_host='uav1')
        mirror=manager._mirror('flight')
        mirror.consume(page('旧日志\n'))
        first=manager.read_log('flight')
        mirror.consume(page('新日志'*100+'\n',session='b'))
        second=manager.read_log('flight',first['offset'],generation=first['generation'])
        self.assertTrue(second['reset'])
        self.assertIn('仅保留',second['text'])
        self.assertIn('新日志',second['text'])
        self.assertNotIn('旧日志',second['text'])

    def test_http_log_api_honors_generation(self):
        config=Path(self.temp.name)/'fleet.json'
        config.write_text(json.dumps(default_fleet()),encoding='utf-8')
        manager=ProgramManager(self.temp.name,{})
        manager.local=dict(uav_id=1,onboard_host='uav1')
        entry=GroundEntry({'COMPETITION_FLEET_CONFIG':str(config)},programs_factory=lambda *args:manager)
        mirror=manager._mirror('detection')
        mirror.consume(page('首次启动\n'))
        with TestClient(entry) as client:
            old=client.get('/api/v1/programs/detection/logs').json()
            mirror.consume(page('下一次启动\n',session='b'))
            new=client.get('/api/v1/programs/detection/logs',params={'offset':old['offset'],'generation':old['generation']}).json()
        self.assertTrue(new['reset'])
        self.assertIn('下一次启动',new['text'])
        self.assertNotIn('首次启动',new['text'])

    def test_watch_migrates_legacy_history_then_restores_cursor(self):
        manager=ProgramManager(self.temp.name,{})
        manager.local=dict(uav_id=1,onboard_host='uav1')
        manager.logs.mkdir(parents=True)
        manager._path('detection').write_text('多次启动的旧日志',encoding='utf-8')
        seen=[]
        def request(action,offsets):
            seen.append(dict(offsets))
            manager.stop_event.set()
            return {key:dict(page('本次日志\n'),state='running') for key in remote.NAMES}
        manager._request=request
        manager._watch()
        self.assertEqual(seen[0]['detection'],0)
        self.assertNotIn('旧日志',manager._path('detection').read_text(encoding='utf-8'))
        reopened=ProgramManager(self.temp.name,{})
        reopened.local=manager.local
        self.assertEqual(reopened._mirror('detection').offset,len('本次日志\n'.encode()))

    def test_log_opened_before_terminal_selection_uses_selected_drone_afterwards(self):
        manager=ProgramManager(self.temp.name,{})
        manager.read_log('flight')
        manager.local=dict(uav_id=3,onboard_host='uav3')
        mirror=manager._mirror('flight')
        mirror.consume(page('三号机\n'))
        self.assertEqual(mirror.path.name,'uav3_flight.log')
        self.assertEqual(mirror.source,'uav3')
        self.assertIn('三号机',manager.read_log('flight')['text'])


class RemoteLatestWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.log=self.directory/'console.log'
        self.a='\n[2026-10-05 10:00:00] 启动指令：旧指令\n旧日志\n'.encode()
        self.b='\n[2026-10-05 11:00:00] 启动指令：新指令\n【配置测试】新日志\n'.encode()

    def status(self,offset=0):
        return remote.status(self.directory,offset,latest_log=True)

    def test_upgrade_skips_all_old_starts_without_modifying_raw_log(self):
        original=self.a+b'x'*100000+self.b
        self.log.write_bytes(original)
        info=self.status()
        text=base64.b64decode(info['log']).decode()
        self.assertNotIn('旧指令',text)
        self.assertIn('新指令',text)
        self.assertEqual(info['log_start'],len(self.a)+100001)
        self.assertEqual(self.log.read_bytes(),original)

    def test_append_new_launch_changes_session_even_after_index_cached(self):
        self.log.write_bytes(self.a)
        first=self.status()
        with self.log.open('ab') as stream: stream.write(b'more\n')
        self.assertEqual(self.status(first['offset'])['log_session'],first['log_session'])
        with self.log.open('ab') as stream: stream.write(self.b)
        second=self.status(first['offset'])
        self.assertNotEqual(second['log_session'],first['log_session'])
        self.assertIn('新日志',base64.b64decode(second['log']).decode())

    def test_latest_marker_across_reverse_scan_chunk_boundary(self):
        # 让 1 MiB 分块的边界落在最新启动标记的正中。
        original=self.a+self.b+b'x'*(1024*1024-len(self.b)+20)
        self.log.write_bytes(original)
        info=self.status()
        self.assertEqual(info['log_start'],len(self.a)+1)
        self.assertTrue(base64.b64decode(info['log']).startswith(self.b[1:]))

    def test_truncated_log_and_old_offset_recover_first_page(self):
        self.log.write_bytes(self.a+b'x'*100000)
        first=self.status()
        self.log.write_bytes(self.b)
        second=self.status(first['offset'])
        self.assertNotEqual(first['log_session'],second['log_session'])
        third=self.status(second['log_start'])
        self.assertIn('新指令',base64.b64decode(third['log']).decode())

    def test_borrowed_process_does_not_claim_old_output(self):
        self.log.write_bytes(self.a)
        with patch.object(remote,'current_record',return_value=dict(borrowed=True,pid=7,stamp='t',boot_id='b')), patch.object(remote,'alive',return_value=True):
            info=self.status()
        self.assertEqual(info['log'],'')
        self.assertTrue(info['log_session'].startswith('borrowed-'))
        self.assertIn('无法追溯',info['detail'])


if __name__=='__main__':
    unittest.main()
