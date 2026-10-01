"""网页规划作业：保留同一请求幂等性，人工重试按最新请求串行分派。"""
from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional


LOGGER = logging.getLogger(__name__)
_CONTEXT = threading.local()


class PlanSuperseded(Exception):
    pass


def plan_checkpoint(stage: str) -> None:
    """在操作边界退出旧作业，不能用超时线程并发执行两个分派。"""
    current = getattr(_CONTEXT, "current", None)
    if current is not None:
        current[0]._checkpoint(current[1], stage)


class PlanJobRegistry:
    def __init__(self, max_run_seconds: float = 45.0, audit=None) -> None:
        self.audit = audit
        self._lock = threading.Lock()
        self._current: Optional[Dict[str, Any]] = None
        self._running: Optional[Dict[str, Any]] = None
        self._queued: Optional[tuple] = None
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self.max_run_seconds = max_run_seconds

    def _record(self, label, job, **details):
        if self.audit:
            self.audit.record(label, job_id=job["job_id"], request_id=job["request_id"],
                              state=job["state"], stage=job["stage"], **details)

    def _checkpoint(self, job, stage):
        with self._lock:
            if job.get("cancel_requested"):
                raise PlanSuperseded("已改用最新人工规划请求；旧作业不再继续分派，请以新任务回执为准")
            if time.monotonic() - job["_run_started"] > self.max_run_seconds:
                raise RuntimeError("规划分派处理超时，停在阶段：{}；已停止后续下发，请核对已收到的任务".format(job["stage"]))
            now = time.monotonic()
            self._record("规划阶段切换", job, next_stage=stage,
                         stage_elapsed_ms=round((now - job.get("_stage_started", job["_run_started"])) * 1000, 1))
            job["_stage_started"] = now
            job["stage"] = stage

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
                "stage": "等待前一作业结束" if self._running is not None else "准备规划",
                "cancel_requested": False,
            }
            self._current = job
            self._jobs[job["job_id"]] = job
            submission = (job, copy.deepcopy(payload), handler)
            if self._running is None:
                self._running = job
                launch = submission
            else:
                self._running["cancel_requested"] = True
                if self._queued is not None:
                    old = self._queued[0]
                    old["state"] = "superseded"
                    old["error"] = "已有更新的规划请求；此排队作业未执行、未下发"
                    old["finished_at"] = time.time()
                    self._record("排队规划已被新请求替换", old, replacement_job_id=job["job_id"])
                self._queued = submission
            result = self._public_locked(job)
            self._record("收到规划请求", job, parameters=payload,
                         waiting_for_job_id=self._running["job_id"] if self._running is not job else None)
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
        job["_run_started"] = time.monotonic()
        _CONTEXT.current = (self, job)
        try:
            plan_checkpoint("检查规划请求")
            result = handler(payload)
            if not isinstance(result, dict) or not (result.get("mission") or {}).get("mission_id"):
                raise RuntimeError("后台未生成有效任务，请检查任务状态")
        except PlanSuperseded as error:
            outcome = ("superseded", None, str(error))
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
        finally:
            _CONTEXT.current = None
        with self._lock:
            job["state"], job["result"], job["error"] = outcome
            job["finished_at"] = time.time()
            self._record("规划作业结束", job, error=job["error"],
                         elapsed_seconds=round(time.monotonic() - job["_run_started"], 3),
                         mission_id=((job["result"] or {}).get("mission") or {}).get("mission_id"))
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
        result = {key: value for key, value in job.items() if not key.startswith('_')}
        if job.get("started_at") is not None:
            result["elapsed_seconds"] = round((job.get("finished_at") or time.time()) - job["started_at"], 1)
        if job["state"] == "queued" and self._running:
            result["waiting_for_stage"] = self._running["stage"]
        return result

    def get(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._current if job_id is None else self._jobs.get(job_id)
            if job is None:
                return None
            return self._public_locked(job)
