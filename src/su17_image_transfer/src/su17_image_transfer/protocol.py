"""Length-prefixed TCP protocol used by the onboard sender and ground receiver."""

import json
import socket
import struct
import zlib
from typing import Any, Dict, Tuple


MAGIC = b"S17I"
ACK_MAGIC = b"S17A"
RESPONSE_MAGIC = b"S17R"
VERSION = 1

# magic, version, metadata length, JPEG length, JPEG CRC32
HEADER = struct.Struct("!4sB3xIII")
# magic, success (1 or 0)
ACK = struct.Struct("!4sB3x")
# magic, success (1 or 0), JSON response length
RESPONSE_HEADER = struct.Struct("!4sB3xI")

MAX_METADATA_BYTES = 1024 * 1024
MAX_JPEG_BYTES = 50 * 1024 * 1024


class ProtocolError(RuntimeError):
    """Raised when a peer sends an invalid frame."""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Receive exactly *size* bytes or raise ConnectionError."""
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("TCP connection closed while receiving data")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def encode_frame(metadata: Dict[str, Any], jpeg: bytes) -> bytes:
    """Serialize one metadata/JPEG frame."""
    metadata_bytes = json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(metadata_bytes) > MAX_METADATA_BYTES:
        raise ProtocolError("metadata is too large")
    if not jpeg or len(jpeg) > MAX_JPEG_BYTES:
        raise ProtocolError("JPEG length is invalid")
    checksum = zlib.crc32(jpeg) & 0xFFFFFFFF
    return HEADER.pack(
        MAGIC, VERSION, len(metadata_bytes), len(jpeg), checksum
    ) + metadata_bytes + jpeg


def receive_frame(sock: socket.socket) -> Tuple[Dict[str, Any], bytes]:
    """Receive and validate one frame from a connected socket."""
    magic, version, metadata_size, jpeg_size, expected_crc = HEADER.unpack(
        recv_exact(sock, HEADER.size)
    )
    if magic != MAGIC:
        raise ProtocolError("invalid frame magic")
    if version != VERSION:
        raise ProtocolError("unsupported protocol version: %d" % version)
    if metadata_size > MAX_METADATA_BYTES:
        raise ProtocolError("metadata exceeds size limit")
    if jpeg_size == 0 or jpeg_size > MAX_JPEG_BYTES:
        raise ProtocolError("JPEG exceeds size limit")

    metadata_bytes = recv_exact(sock, metadata_size)
    jpeg = recv_exact(sock, jpeg_size)
    actual_crc = zlib.crc32(jpeg) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ProtocolError("JPEG CRC32 mismatch")

    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid metadata JSON: %s" % exc) from exc
    if not isinstance(metadata, dict):
        raise ProtocolError("metadata must be a JSON object")
    return metadata, jpeg


def send_ack(sock: socket.socket, success: bool) -> None:
    sock.sendall(ACK.pack(ACK_MAGIC, 1 if success else 0))


def receive_ack(sock: socket.socket) -> bool:
    magic, success = ACK.unpack(recv_exact(sock, ACK.size))
    if magic != ACK_MAGIC:
        raise ProtocolError("invalid ACK magic")
    return success == 1


def send_response(sock: socket.socket, success: bool, data: Dict[str, Any]) -> None:
    """Send a structured response, used by manifest reconciliation."""
    payload = json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_METADATA_BYTES:
        raise ProtocolError("response is too large")
    sock.sendall(RESPONSE_HEADER.pack(RESPONSE_MAGIC, 1 if success else 0, len(payload)))
    sock.sendall(payload)


def receive_response(sock: socket.socket) -> Tuple[bool, Dict[str, Any]]:
    """Receive a structured response from the ground receiver."""
    magic, success, payload_size = RESPONSE_HEADER.unpack(
        recv_exact(sock, RESPONSE_HEADER.size)
    )
    if magic != RESPONSE_MAGIC:
        raise ProtocolError("invalid response magic")
    if payload_size > MAX_METADATA_BYTES:
        raise ProtocolError("response exceeds size limit")
    try:
        data = json.loads(recv_exact(sock, payload_size).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid response JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise ProtocolError("response must be a JSON object")
    return success == 1, data
