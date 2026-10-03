"""真实 TCP 丢包代理模拟断电，无需飞机、ROS、管理员网卡操作。"""
import json
from pathlib import Path
import select
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src/su17_competition_executor/src'))
from su17_competition_executor.tcp_link import OnboardTcpLink
from competition_backend.tcp_adapter import TcpFleetAdapter


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


class BlackholeProxy:
    def __init__(self, target):
        self.target = target
        self.drop_up = self.drop_down = False
        self.stopped = threading.Event()
        self.sockets = []
        self.accepted = 0
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(16)
        self.listener.settimeout(.1)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                left, _ = self.listener.accept()
                right = socket.create_connection(self.target, timeout=1)
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            self.sockets.extend([left, right])
            threading.Thread(target=self.relay, args=(left, right), daemon=True).start()

    def relay(self, left, right):
        try:
            while not self.stopped.is_set():
                readable, _, _ = select.select([left, right], [], [], .1)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    dropped = self.drop_up if source is left else self.drop_down
                    if not dropped:
                        (right if source is left else left).sendall(data)
        except OSError:
            pass
        finally:
            for connection in (left, right):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

    def close(self):
        self.stopped.set()
        self.listener.close()
        for connection in self.sockets:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.thread.join(1)


class ReconnectTest(unittest.TestCase):
    def test_clock_revisions_and_return_event_survive_real_transport(self):
        messages=[]
        clock=dict(running=True,synchronized=True,session_id='session',elapsed_seconds=1400,
                   revision=0,publisher_terminal_id=1,updated_by=1)
        server=TcpFleetAdapter([2],bind_host='127.0.0.1',port=0)
        server.competition_time_provider=lambda:dict(clock)
        server.start()
        link=OnboardTcpLink(2,'127.0.0.1',server.bound_port,'',messages.append)
        try:
            link.start()
            self.assertTrue(wait_for(lambda:any(m.get('type')=='sync_competition_time' for m in messages)))
            clock.update(revision=1,elapsed_seconds=1350)
            self.assertTrue(wait_for(lambda:any(m.get('competition_time',{}).get('revision')==1 for m in messages)))
            event=dict(phase='return_descent',uav_id=2,mission_id='subject1-a',
                       competition_session_id='session',assignment_checksum='checksum',event_id='event')
            link.update_telemetry(dict(type='telemetry',uav_id=2,connected=True,successful_return=event,
                                       position=[0,0,2],velocity=[0,0,-.1]))
            self.assertTrue(wait_for(lambda:server.telemetry_snapshot(2).successful_return.get('event_id')=='event'))
            self.assertTrue(all(m['type']=='sync_competition_time' for m in messages))
        finally:
            link.stop();server.stop()

    def test_busy_ground_planner_does_not_block_heartbeats_or_return_commands(self):
        blocked, release = threading.Event(), threading.Event()
        commands = []
        with patch('su17_competition_executor.tcp_link.HEARTBEAT_INTERVAL_SECONDS', .1), \
             patch('su17_competition_executor.tcp_link.HEARTBEAT_TIMEOUT_SECONDS', .6), \
             patch('competition_backend.tcp_adapter.LINK_IDLE_TIMEOUT_SECONDS', .6):
            server = TcpFleetAdapter([1], bind_host='127.0.0.1', port=0)
            def busy_sink(telemetry):
                blocked.set()
                release.wait(5)
            server.set_telemetry_sink(busy_sink)
            server.start()
            link = OnboardTcpLink(1, '127.0.0.1', server.bound_port, '', commands.append)
            try:
                link.start()
                self.assertTrue(wait_for(lambda: link.connected))
                link.update_telemetry(dict(type='telemetry', uav_id=1, connected=True,
                                           position=[1, 2, 3], velocity=[0, 0, 0]))
                self.assertTrue(blocked.wait(1))
                time.sleep(1.2)
                self.assertTrue(link.connected)
                server.command_return(1, dict(mission_id='flight', reason='manual'))
                self.assertTrue(wait_for(lambda: len(commands) == 1))
                self.assertEqual('return_home', commands[0]['type'])
            finally:
                release.set()
                link.stop()
                server.stop()

    def test_blackhole_then_return_command_without_task_replay(self):
        # 包含双向失联和单向失联，socket 全程不会由代理主动断开。
        for direction in ('both', 'up', 'down'):
            with self.subTest(direction=direction), \
                 patch('su17_competition_executor.tcp_link.HEARTBEAT_INTERVAL_SECONDS', .1), \
                 patch('su17_competition_executor.tcp_link.HEARTBEAT_TIMEOUT_SECONDS', .8), \
                 patch('competition_backend.tcp_adapter.LINK_IDLE_TIMEOUT_SECONDS', 1.0):
                server = TcpFleetAdapter([2], bind_host='127.0.0.1', port=0, auth_token='test')
                server.start()
                proxy = BlackholeProxy(('127.0.0.1', server.bound_port))
                commands = []
                link = OnboardTcpLink(2, '127.0.0.1', proxy.port, 'test', commands.append, reconnect_seconds=.2)
                stopped = threading.Event()
                point = [1]

                def telemetry():
                    while not stopped.wait(.05):
                        link.update_telemetry(dict(type='telemetry', uav_id=2, connected=True,
                            armed=True, position=[point[0], 2, 3], velocity=[0, 0, 0],
                            task_phase='external_waiting', task_complete=False,
                            task_assignment_acked=True, task_assignment_mission_id='flight',
                            task_assignment_checksum='unchanged'))

                producer = threading.Thread(target=telemetry, daemon=True)
                producer.start()
                link.start()
                try:
                    self.assertTrue(wait_for(lambda: link.connected and server.telemetry_snapshot(2) is not None))
                    server.command_assign_task(2, dict(mission_id='flight', assignment_checksum='unchanged'))
                    self.assertTrue(wait_for(lambda: len(commands) == 1))
                    before = proxy.accepted
                    proxy.drop_up = direction in ('both', 'up')
                    proxy.drop_down = direction in ('both', 'down')
                    self.assertTrue(wait_for(lambda: not link.connected))
                    point[0] = 99
                    proxy.drop_up = proxy.drop_down = False
                    self.assertTrue(wait_for(lambda: link.connected and proxy.accepted > before
                        and server.telemetry_snapshot(2).connected
                        and server.telemetry_snapshot(2).position[0] == 99, 8))
                    server.command_return(2, dict(mission_id='flight', reason='operator'))
                    self.assertTrue(wait_for(lambda: len(commands) == 2))
                    self.assertEqual(['assign_task', 'return_home'], [m['type'] for m in commands])
                    snapshot = server.telemetry_snapshot(2)
                    self.assertEqual('flight', snapshot.task_assignment_mission_id)
                    self.assertEqual('external_waiting', snapshot.task_phase)
                finally:
                    stopped.set()
                    producer.join(1)
                    link.stop()
                    proxy.close()
                    server.stop()

    def test_heartbeats_keep_transport_alive_without_inventing_telemetry(self):
        with patch('su17_competition_executor.tcp_link.HEARTBEAT_INTERVAL_SECONDS', .1), \
             patch('competition_backend.tcp_adapter.LINK_IDLE_TIMEOUT_SECONDS', .6):
            server = TcpFleetAdapter([1], bind_host='127.0.0.1', port=0)
            server.start()
            link = OnboardTcpLink(1, '127.0.0.1', server.bound_port, '', lambda m: None)
            try:
                link.start()
                self.assertTrue(wait_for(lambda: link.connected))
                link.update_telemetry(dict(type='telemetry', uav_id=1, connected=True,
                                           position=[1, 2, 3], velocity=[0, 0, 0]))
                self.assertTrue(wait_for(lambda: server.telemetry_snapshot(1).gps_telemetry_age_seconds is not None))
                received_at = server.telemetry_snapshot(1).received_at
                time.sleep(1.2)
                self.assertTrue(link.connected)
                self.assertEqual([1], server.connected_uav_ids)
                self.assertEqual(received_at, server.telemetry_snapshot(1).received_at)
                self.assertGreater(server.telemetry_snapshot(1).gps_telemetry_age_seconds, 1)
            finally:
                link.stop()
                server.stop()


if __name__ == '__main__':
    unittest.main()
