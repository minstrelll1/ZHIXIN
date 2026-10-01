"""地面运行诊断：即时落盘、按启动分组，不记录认证信息和原始媒体。"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


def safe(value, key="", depth=0, compact=True):
    if any(word in key.lower() for word in ("token", "password", "secret", "authorization", "private_key")):
        return "[已隐藏]"
    if depth > (6 if compact else 40):
        return "[层级省略]"
    if isinstance(value, dict):
        items = list(value.items())[:80] if compact else value.items()
        return {str(k): safe(v, str(k), depth + 1, compact) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [safe(v, depth=depth + 1, compact=compact) for v in (value[:24] if compact else value)] + (["[其余省略，共%d项]" % len(value)] if compact and len(value) > 24 else [])
    if isinstance(value, str):
        value = re.sub(r"(?i)(token|password|authorization)([\s\"':=]+)[^\s,;\"}]+", r"\1\2[已隐藏]", value)
        value = re.sub(r"(?i)(https?|rtsp)://[^/@\s]+:[^/@\s]+@", r"\1://[已隐藏]@", value)
        return value[:2000] if compact else value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe(str(value))


class Diagnostics:
    def __init__(self, root):
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.directory = Path(root) / self.session_id
        self._lock = threading.RLock()
        self._handler = None
        self._closed = False
        self._initialized = False
        self.context = {}

    def _initialize(self):
        if self._initialized:
            return
        self._initialized = True
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._handler = RotatingFileHandler(self.directory / "events.jsonl", maxBytes=20 * 1024 * 1024,
                                               backupCount=4, encoding="utf-8")
            self._handler.setFormatter(logging.Formatter("%(message)s"))
        except OSError as error:
            logging.getLogger(__name__).error("诊断日志无法创建：%s", error)
        if self._handler:
            self.record("程序会话开始", pid=os.getpid())

    def record(self, event, **details):
        # 诊断失败不能让飞行操作失败；每条输出均 flush，不等退出才写文件。
        try:
            with self._lock:
                if self._closed:
                    return
                self._initialize()
                if self._handler is None:
                    return
                body = dict(time=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                            unix_time=time.time(), session_id=self.session_id, **self.context)
                body.update(event=event, details=safe(details))
                self._handler.emit(logging.LogRecord("diagnostics", logging.INFO, "", 0,
                                   json.dumps(body, ensure_ascii=False), (), None))
        except Exception:
            logging.getLogger(__name__).exception("诊断日志写入失败，业务继续运行")

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._handler:
                self.record("程序会话正常结束")
            self._closed = True
            if self._handler:
                self._handler.close()

    def save_assignment(self, uav_id, payload):
        """另存完整下发内容，避免摘要截断航点；不作为任务恢复输入。"""
        try:
            with self._lock:
                if self._closed:
                    return None
                self._initialize()
                if self._handler is None:
                    return None
                name = 'task_uav%d_%s.json' % (int(uav_id), uuid.uuid4().hex[:12])
                path = self.directory / name
                path.write_text(json.dumps(safe(payload, compact=False), ensure_ascii=False, indent=2), encoding='utf-8')
                return name
        except Exception:
            logging.getLogger(__name__).exception("任务诊断副本写入失败，业务继续运行")
            return None


class DiagnosticMiddleware:
    """旁路观察 ASGI 消息，不提前消费请求、不改响应、不输出轮询或媒体帧。"""
    def __init__(self, app, audit):
        self.app, self.audit = app, audit

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        quiet = ("/api/v1/traffic/report", "/api/v1/peer/lease", "/api/v1/peer/mission")
        if (scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "DELETE", "PATCH")
                or not path.startswith("/api/") or path in quiet or "/ingest" in path or "/sim/" in path):
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        start = time.monotonic()
        request_body, response_body = bytearray(), bytearray()
        request_size = response_size = 0
        status = None
        self.audit.record("操作请求开始", request_id=request_id, method=scope["method"], path=path,
                          client=(scope.get("client") or [None])[0])
        async def read():
            nonlocal request_size
            message = await receive()
            chunk = message.get("body", b"")
            request_size += len(chunk)
            request_body.extend(chunk[:max(0, 65536 - len(request_body))])
            return message
        async def write(message):
            nonlocal status, response_size
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                response_size += len(chunk)
                response_body.extend(chunk[:max(0, 65536 - len(response_body))])
            await send(message)
        def summary(raw, size):
            if size > 65536:
                return {"bytes": size, "detail": "大请求或响应省略；详见业务事件"}
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    # 文件内容和批量原始数据不复制到诊断日志。
                    return {k: v for k, v in value.items() if k not in ("document", "config", "telemetry", "points")}
                return {"bytes": size}
            except (ValueError, UnicodeError):
                return {"bytes": size}
        try:
            await self.app(scope, read, write)
        except BaseException as error:
            self.audit.record("操作请求异常", request_id=request_id, path=path,
                              error_type=type(error).__name__, error=str(error))
            raise
        finally:
            self.audit.record("操作请求结束", request_id=request_id, path=path, http_status=status,
                              elapsed_ms=round((time.monotonic() - start) * 1000, 1),
                              request=summary(request_body, request_size), response=summary(response_body, response_size))
