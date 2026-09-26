"""在 Windows 复制 GroundStation 已接收的 IPv4/TCP 数据，不建立机载连接。"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import socket
import struct
import threading
import time
from pathlib import Path

MAX_BUFFER = 64 * 1024 * 1024
LOG = logging.getLogger("uvicorn.error.groundstation_pointcloud")


def tcp_packet(packet: bytes):
    """解析完整 IPv4 TCP 包；分片及截断包不参与流重组。"""
    if len(packet) < 20 or packet[0] >> 4 != 4 or packet[9] != 6:
        return None
    ihl = (packet[0] & 15) * 4
    size = struct.unpack_from("!H", packet, 2)[0]
    flags = struct.unpack_from("!H", packet, 6)[0]
    if ihl < 20 or size > len(packet) or size < ihl + 20 or flags & 0x3fff:
        return None
    source, target = socket.inet_ntoa(packet[12:16]), socket.inet_ntoa(packet[16:20])
    source_port, target_port, sequence = struct.unpack_from("!HHI", packet, ihl)
    tcp_size = (packet[ihl + 12] >> 4) * 4
    if tcp_size < 20 or ihl + tcp_size > size:
        return None
    return (source, source_port, target, target_port), sequence, packet[ihl + 13], packet[ihl + tcp_size:size]


class WebSocketMessages:
    """从任意抓取位置寻找服务器 JSON 帧，支持跨包及 WebSocket 分片。"""
    def __init__(self):
        self.buffer = bytearray()
        self.fragment = None
        self.synced = False

    def feed(self, data: bytes):
        self.buffer.extend(data)
        if len(self.buffer) > MAX_BUFFER:
            self.buffer.clear()
            self.fragment = None
            self.synced = False
            raise ValueError("WebSocket 重组缓存超出限制，已等待下一完整帧")
        output = []
        while len(self.buffer) >= 2:
            if not self.synced:
                candidates = [index for index in (self.buffer.find(b"\x81"), self.buffer.find(b"\x01")) if index >= 0]
                if not candidates:
                    self.buffer.clear()
                    break
                del self.buffer[:min(candidates)]
                if len(self.buffer) < 2:
                    break
            first, second = self.buffer[:2]
            opcode, final = first & 15, bool(first & 128)
            if (first & 0x70 or second & 128 or opcode not in (0, 1, 8, 9, 10) or
                    (not self.synced and opcode != 1)):
                del self.buffer[0]
                self.fragment = None
                self.synced = False
                continue
            size, header = second, 2
            if size in (126, 127):
                header = 4 if size == 126 else 10
                if len(self.buffer) < header:
                    break
                size = int.from_bytes(self.buffer[2:header], "big")
            if size > MAX_BUFFER - header or (opcode >= 8 and (size > 125 or not final)):
                del self.buffer[0]
                self.synced = False
                self.fragment = None
                continue
            # ROSBridge 的文本发布是 JSON 对象；过滤抓包开头的残帧和 HTTP 握手。
            if not self.synced and len(self.buffer) > header and self.buffer[header] != ord('{'):
                del self.buffer[0]
                continue
            if len(self.buffer) < header + size:
                break
            payload = bytes(self.buffer[header:header + size])
            del self.buffer[:header + size]
            self.synced = True
            if opcode >= 8:
                if opcode == 8:
                    self.fragment = None
                    self.synced = False
                continue
            if opcode == 1:
                self.fragment = bytearray(payload)
            elif self.fragment is not None:
                self.fragment.extend(payload)
            else:
                continue
            if len(self.fragment) > MAX_BUFFER:
                self.fragment = None
                raise ValueError("WebSocket 分片消息超过限制")
            if final:
                complete, self.fragment = self.fragment, None
                try:
                    value = json.loads(complete)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    output.append(value)
        return output


class TcpStream:
    def __init__(self):
        self.expected = None
        self.pending = []
        self.pending_bytes = 0
        self.gap_since = None
        self.websocket = WebSocketMessages()
        self.resets = 0

    def feed(self, sequence: int, payload: bytes, now: float):
        if not payload:
            return []
        if self.expected is None:
            self.expected = sequence
        delta = ((sequence - self.expected + (1 << 31)) % (1 << 32)) - (1 << 31)
        if delta > 0:
            if self.gap_since is None:
                self.gap_since = now
            if now - self.gap_since < 2 and self.pending_bytes + len(payload) <= MAX_BUFFER:
                self.pending.append((sequence, payload))
                self.pending_bytes += len(payload)
                return []
            # 缺失包不拼接为假帧；丢弃该段并从后续完整 JSON 帧恢复。
            self.websocket = WebSocketMessages()
            self.pending.clear()
            self.pending_bytes = 0
            self.expected = sequence
            self.gap_since = None
            self.resets += 1
            delta = 0
        payload = payload[-delta:]
        if not payload:
            return []
        self.expected = (self.expected + len(payload)) % (1 << 32)
        result = self.websocket.feed(payload)
        while self.pending:
            candidates = [(((seq - self.expected + (1 << 31)) % (1 << 32)) - (1 << 31), i)
                          for i, (seq, _) in enumerate(self.pending)]
            offset, index = min(candidates)
            if offset > 0:
                break
            seq, part = self.pending.pop(index)
            self.pending_bytes -= len(part)
            part = part[-offset:]
            if part:
                self.expected = (self.expected + len(part)) % (1 << 32)
                result.extend(self.websocket.feed(part))
        if not self.pending:
            self.gap_since = None
        return result


class GroundStationStream:
    def __init__(self, local_ip: str, remote_ip: str, remote_port: int = 9090):
        self.local_ip = str(ipaddress.IPv4Address(local_ip))
        self.remote_ip = str(ipaddress.IPv4Address(remote_ip))
        if not 1 <= remote_port <= 65535:
            raise ValueError("机载端口必须在 1～65535 之间")
        self.remote_port = remote_port
        self.flows = {}
        self.packet_count = self.message_count = self.gap_resets = 0

    def feed(self, packet: bytes, now=None):
        item = tcp_packet(packet)
        if item is None:
            return []
        key, sequence, flags, payload = item
        if key[:3] != (self.remote_ip, self.remote_port, self.local_ip):
            return []
        self.packet_count += 1
        if key not in self.flows or flags & 2:
            if len(self.flows) >= 8:
                self.flows.pop(next(iter(self.flows)))
            self.flows[key] = TcpStream()
        stream = self.flows[key]
        before = stream.resets
        result = stream.feed((sequence + bool(flags & 2)) % (1 << 32), payload,
                             time.monotonic() if now is None else now)
        self.gap_resets += stream.resets - before
        if flags & 5:
            self.flows.pop(key, None)
        self.message_count += len(result)
        return result


def pcapng_ipv4(path: Path):
    """流式读取 pktmon 生成的 pcapng，不需要 Wireshark 或 Npcap。"""
    endian, interfaces = "<", []
    with Path(path).open("rb") as stream:
        while True:
            head = stream.read(12)
            if not head:
                return
            if len(head) != 12:
                raise ValueError("pcapng 文件头不完整")
            if head[:4] == b"\x0a\x0d\x0d\x0a":
                if head[8:] == b"\x4d\x3c\x2b\x1a":
                    endian = "<"
                elif head[8:] == b"\x1a\x2b\x3c\x4d":
                    endian = ">"
                else:
                    raise ValueError("pcapng 字节序标记无效")
                interfaces = []
            kind, size = struct.unpack(endian + "II", head[:8])
            if not 12 <= size <= MAX_BUFFER or size % 4:
                raise ValueError("pcapng 数据块长度无效")
            block = head + stream.read(size - 12)
            if len(block) != size or struct.unpack(endian + "I", block[-4:])[0] != size:
                raise ValueError("pcapng 数据块不完整")
            if kind == 1:
                interfaces.append(struct.unpack_from(endian + "H", block, 8)[0])
            elif kind == 6:
                if size < 32:
                    raise ValueError("pcapng 数据包头不完整")
                interface, _, _, captured, original = struct.unpack_from(endian + "5I", block, 8)
                if interface >= len(interfaces) or captured > size - 32 or captured > original:
                    raise ValueError("pcapng 数据包长度或网卡编号无效")
                if captured != original:
                    raise ValueError("抓包截断了数据，请使用 pktmon --pkt-size 0 重新抓取")
                packet = block[28:28 + captured]
                link_type = interfaces[interface]
                if link_type == 1:
                    if len(packet) < 14:
                        continue
                    offset, protocol = 14, packet[12:14]
                    while protocol in (b"\x81\x00", b"\x88\xa8") and len(packet) >= offset + 4:
                        protocol = packet[offset + 2:offset + 4]
                        offset += 4
                    if protocol == b"\x08\x00":
                        yield packet[offset:]
                elif link_type in (101, 228):
                    yield packet
                else:
                    raise ValueError("暂不支持该 pcapng 网卡链路类型：{}".format(link_type))


class PassivePointCloudCapture:
    def __init__(self, local_ip, remote_ip, remote_port, uav_id, topic, ingest):
        self.stream = GroundStationStream(local_ip, remote_ip, remote_port)
        self.uav_id, self.topic, self.ingest = uav_id, topic, ingest
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=1)
        self._threads = []
        self._socket = None
        self.running = False
        self.error = ""
        self.decoded_frames = self.skipped_frames = 0
        self.last_received_at = None

    def status(self):
        return {"enabled": True, "running": self.running, "error": self.error,
                "uav_id": self.uav_id, "topic": self.topic,
                "local_ip": self.stream.local_ip, "remote_ip": self.stream.remote_ip,
                "remote_port": self.stream.remote_port,
                "packets": self.stream.packet_count, "messages": self.stream.message_count,
                "decoded_frames": self.decoded_frames, "skipped_frames": self.skipped_frames,
                "gap_resets": self.stream.gap_resets, "last_received_at": self.last_received_at}

    def start(self):
        self._stop.clear()
        self._threads = [threading.Thread(target=target, daemon=True, name=name) for target, name
                         in ((self._capture, "groundstation-capture"), (self._decode, "groundstation-decode"))]
        for thread in self._threads:
            thread.start()

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads = []

    def _capture(self):
        receiver = None
        try:
            if os.name != "nt":
                raise OSError("此接收方式需要 Windows 地面电脑")
            receiver = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
            self._socket = receiver
            receiver.bind((self.stream.local_ip, 0))
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            receiver.settimeout(.5)
            receiver.ioctl(socket.SIO_RCVALL, socket.RCVALL_ON)
            self.running = True
            LOG.info("地面点云接收已启动：%s:%s → %s，网页 UAV%s，等待 GroundStation 数据",
                     self.stream.remote_ip, self.stream.remote_port, self.stream.local_ip, self.uav_id)
            while not self._stop.is_set():
                try:
                    packet = receiver.recv(65535)
                except socket.timeout:
                    continue
                try:
                    messages = self.stream.feed(packet)
                except ValueError as error:
                    self.error = str(error)
                    continue
                for payload in messages:
                    received_topic = str(payload.get("topic", ""))
                    received_base = received_topic[:-11] if received_topic.endswith("/compressed") else received_topic
                    configured_base = self.topic[:-11] if self.topic.endswith("/compressed") else self.topic
                    if payload.get("op") != "publish" or received_base != configured_base:
                        continue
                    self.last_received_at = time.time()
                    payload["_competition_capture"] = "groundstation_live"
                    if self._queue.full():
                        try:
                            self._queue.get_nowait()
                            self.skipped_frames += 1
                        except queue.Empty:
                            pass
                    self._queue.put_nowait(payload)
        except OSError as error:
            self.error = ("地面抓取启动失败：请用管理员 PowerShell 启动，并确认本机网卡地址为 {}。{}"
                          .format(self.stream.local_ip, error))
            LOG.error(self.error)
        finally:
            self.running = False
            if receiver is not None:
                try:
                    receiver.ioctl(socket.SIO_RCVALL, socket.RCVALL_OFF)
                except OSError:
                    pass
                receiver.close()
            self._socket = None

    def _decode(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=.5)
            except queue.Empty:
                continue
            try:
                frame = self.ingest(self.uav_id, payload)
                self.decoded_frames += 1
                self.error = ""
                if self.decoded_frames == 1 or self.decoded_frames % 30 == 0:
                    LOG.info("地面点云已更新：UAV%s，第 %s 帧，原始 %s 点，显示 %s 点",
                             self.uav_id, self.decoded_frames, frame["source_points"], frame["sampled_points"])
            except Exception as error:
                self.error = "点云解压失败：{}".format(error)
                LOG.warning(self.error)
