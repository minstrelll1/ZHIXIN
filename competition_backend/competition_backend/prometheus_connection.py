"""Prometheus 单机连接协议；只允许 UAVBASIC/CREATE，不包含飞行指令。"""
import json
import socket
import struct
import threading
import time


def crc16(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
    return value


def connect_packet(uid):
    if type(uid) is not int or uid not in range(1, 7):
        raise ValueError('无人机编号必须为 1～6')
    body = json.dumps(dict(cmd='', is_simulation=False, mode=1, selectId=[uid],
                           swarm_num=0, use_mode=0), sort_keys=True, separators=(',', ':')).encode('ascii')
    data = b'am' + struct.pack('<IBB', len(body), 202, uid) + body
    return data + struct.pack('<H', crc16(data))


def decode_packet(data):
    if len(data) < 10 or data[:2] != b'am':
        raise ValueError('无效厂商报文')
    size, kind, uid = struct.unpack('<IBB', data[2:8])
    if size > 1048566 or len(data) != size + 10 or crc16(data[:-2]) != int.from_bytes(data[-2:], 'little'):
        raise ValueError('厂商报文长度或校验和错误')
    return kind, uid, json.loads(data[8:-2].decode('utf-8'))


class PrometheusConnection:
    """先建立真实地面心跳接收，再连接；重连只复用厂商已创建的 UAVBasic。"""
    def __init__(self, local):
        self.local = local
        self.socket = None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_heartbeat = 0.0
        self.heartbeat_count = None
        self.last_connect = 0.0
        self.boot_id = ''
        self.external = False
        self.bound = None
        self.status = dict(state='starting', detail='等待厂商基础通信检查', ready=False)

    def _listen(self, address, port):
        if self.bound == (address, port) and self.socket:
            return
        if self.socket:
            raise RuntimeError('厂商心跳地址已改变，请重开地面程序')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((address, port))
            listener.listen(16)
            listener.settimeout(.5)
        except OSError:
            listener.close()
            raise
        self.socket, self.bound = listener, (address, port)
        self.thread = threading.Thread(target=self._receive, name='prometheus-heartbeat', daemon=True)
        self.thread.start()

    def _receive(self):
        while not self.stop_event.is_set():
            try:
                connection, peer = self.socket.accept()
                with connection:
                    connection.settimeout(1)
                    data = bytearray()
                    if peer[0] != self.local['onboard_host']:
                        continue
                    while len(data) <= 1048576:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                        if len(data) >= 8 and len(data) >= int.from_bytes(data[2:6], 'little') + 10:
                            break
                    kind, uid, body = decode_packet(data)
                    if kind == 6 and uid == self.local['uav_id']:
                        self.last_heartbeat = time.monotonic()
                        count = body.get('count') if isinstance(body, dict) else None
                        self.heartbeat_count = count if type(count) is int and count >= 0 else None
            except (OSError, ValueError, TypeError):
                continue

    def _connect(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            # 双网口电脑不能让 Windows 选到交换机或同网段的另一张网卡。
            sock.bind((self.local.get('ground_host', '192.168.1.230'), 0))
            sock.connect((self.local['onboard_host'], port))
            sock.sendall(connect_packet(self.local['uav_id']))
            sock.shutdown(socket.SHUT_WR)
        self.last_connect = time.monotonic()

    def step(self, probe, switch_gps):
        """probe 来自机载只读快照；switch_gps 内部再次校验未解锁和开机标识。"""
        ready, detail = False, ''
        preparation_audit = probe.get('preparation_audit')
        uid = self.local['uav_id']
        if probe['boot_id'] != self.boot_id:
            self.boot_id = probe['boot_id']
            self.last_connect = self.last_heartbeat = 0.0
            self.heartbeat_count = None
        bridge = probe.get('bridge', {})
        if not bridge or int(bridge.get('ROBOT_ID', 0)) != uid or int(bridge.get('uav_id', 0)) != uid:
            raise RuntimeError('厂商通信桥未运行或编号不匹配，未发送连接命令')
        if int(bridge.get('is_simulation', 1)) != 0:
            raise RuntimeError('厂商通信桥仍为仿真模式，未发送实机连接命令')
        ground = self.local.get('ground_host', '192.168.1.230')
        try:
            self._listen(ground, int(bridge.get('tcp_heartbeat_port', 55556)))
            self.external = False
        except OSError as error:
            # 已打开原地面站时只复用它的完整连接；不抢端口、不结束它的进程。
            occupied = getattr(error, 'winerror', None) == 10048 or error.errno in (48, 98, 10048)
            if not occupied:
                raise RuntimeError('无法监听厂商心跳，请检查图传网卡地址 %s：%s' % (ground, error)) from error
            self.external = True
        state = probe.get('flight', {})
        heartbeat = time.monotonic() - self.last_heartbeat < 4
        if self.external:
            if not (probe.get('registered') and state.get('connected')):
                raise RuntimeError('厂商心跳端口被其他程序占用且连接未就绪；请关闭原地面站后重试')
        elif not heartbeat and time.monotonic() - self.last_connect > 8:
            if not probe.get('registered'):
                if probe.get('base_present') or probe.get('business_present') or probe.get('prepared'):
                    raise RuntimeError('基础程序已存在但厂商连接对象缺失；保留现有程序，不重复启动基础飞控')
                if not bridge.get('autoload') or not bridge.get('uav_control_start'):
                    raise RuntimeError('厂商未配置基础程序自动启动，请检查通信桥 autoload/uav_control_start')
            # UAVBasic 已存在时厂商只恢复心跳，不执行 uav_control_start。
            self._connect(int(bridge.get('tcp_port', 55555)))
        if not heartbeat and not self.external:
            detail = '已请求厂商连接，等待 UAV%d 心跳（TCP %s）' % (uid, bridge.get('tcp_heartbeat_port', 55556))
        elif not state.get('connected'):
            detail = '厂商心跳已连接，等待 MAVROS 和飞控状态'
        elif probe.get('prepared'):
            ready, detail = True, '本次开机基础连接已准备；重连只恢复心跳与监控'
        elif state.get('armed') or probe.get('business_present'):
            # 接管已有飞行/业务会话只监控，绝不切定位源或补启动缺失程序。
            ready, detail = True, '复用已有运行会话，不切换定位源、不自动补启动程序'
        else:
            result = switch_gps(self.boot_id)
            ready, detail = result.get('ready', False), result['detail']
            preparation_audit = result.get('preparation_audit', preparation_audit)
        self.status = dict(state='running' if ready else 'starting', ready=ready,
                           detail=detail, boot_id=self.boot_id, heartbeat=heartbeat,
                           external_listener=self.external,
                           preparation_audit=preparation_audit,
                           auto_start_allowed=ready and not state.get('armed') and not probe.get('business_present'))
        return self.status

    def heartbeat_snapshot(self):
        """展示真实厂商 TCP 心跳；不以 GPS/任务遥测/SSH 状态代替它。"""
        result = dict(state='unknown', age_seconds=None, timeout_seconds=4.0,
                      count=self.heartbeat_count, source='prometheus_tcp',
                      detail='尚未建立厂商心跳监听')
        if self.external:
            result['detail'] = '心跳由其他地面站接收，本程序无法直接判断'
        elif self.socket and not self.stop_event.is_set():
            if self.last_heartbeat:
                age = max(0.0, time.monotonic() - self.last_heartbeat)
                result.update(state='normal' if age < 4.0 else 'abnormal', age_seconds=age,
                              detail='厂商心跳正常' if age < 4.0 else '超过 4 秒未收到厂商心跳')
            else:
                result.update(state='waiting', detail='等待首个厂商心跳包')
        return result

    def close(self):
        # 关闭地面端不发送 MODE_DELETE、停机或飞行指令。
        self.stop_event.set()
        if self.socket:
            self.socket.close()
        if self.thread:
            self.thread.join(2)
