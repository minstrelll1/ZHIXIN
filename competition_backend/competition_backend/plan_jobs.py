"""网页规划作业：保留同一请求幂等性，人工重试按最新请求串行分派。"""
from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional


LOGGER = logging.getLogger(__name__)


class PlanJobRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[Dict[str, Any]] = None
        self._running: Optional[Dict[str, Any]] = None
        self._queued: Optional[tuple] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def submit(self, payload: Dict[str, Any], handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        request_id = str(payload.get("request_id", ""))
        launch = None
        with self._lock:
            if request_id:
                for existing in self._jobs.values():
                    if existing["request_id"] == request_id:
                        return self._public_locked(existing)
            job = {
                "job_id": uuid.uuid4().hex,
                "request_id": request_id,
                "state": "queued" if self._running is not None else "running",
                "started_at": None if self._running is not None else time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._current = job
            self._jobs[job["job_id"]] = job
            submission = (job, copy.deepcopy(payload), handler)
            if self._running is None:
                self._running = job
                launch = submission
            else:
                if self._queued is not None:
                    old = self._queued[0]
                    old["state"] = "superseded"
                    old["error"] = "已有更新的规划请求；此排队作业未执行、未下发"
                    old["finished_at"] = time.time()
                self._queued = submission
            result = self._public_locked(job)
        if launch is not None:
            self._start(*launch)
        return result

    def _start(self, job: Dict[str, Any], payload: Dict[str, Any], handler: Callable) -> None:
        threading.Thread(
            target=self._run, args=(job, payload, handler),
            name="competition-plan-dispatch", daemon=True,
        ).start()

    def _run(self, job: Dict[str, Any], payload: Dict[str, Any], handler: Callable) -> None:
        next_job = None
        try:
            result = handler(payload)
            if not isinstance(result, dict) or not (result.get("mission") or {}).get("mission_id"):
                raise RuntimeError("后台未生成有效任务，请检查任务状态")
        except Exception as error:
            # FastAPI HTTPException.detail 与普通任务错误均保持为可读文本。
            detail = getattr(error, "detail", None)
            if detail is None:
                LOGGER.exception("规划分派作业 %s 异常", job["job_id"])
            else:
                LOGGER.warning("规划分派作业 %s 失败：%s", job["job_id"], detail)
            outcome = ("failed", None, str(detail if detail is not None else error))
        else:
            outcome = ("succeeded", result, None)
        with self._lock:
            job["state"], job["result"], job["error"] = outcome
            job["finished_at"] = time.time()
            if self._running is job:
                self._running = None
                if self._queued is not None:
                    next_job = self._queued
                    self._queued = None
                    next_job[0]["state"] = "running"
                    next_job[0]["started_at"] = time.time()
                    self._running = next_job[0]
        if next_job is not None:
            self._start(*next_job)

    def _public_locked(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return dict(job)

    def get(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._current if job_id is None else self._jobs.get(job_id)
            if job is None:
                return None
            return self._public_locked(job)
