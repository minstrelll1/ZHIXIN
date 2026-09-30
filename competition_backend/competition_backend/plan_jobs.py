"""网页规划作业：请求结束不代表分派结束，作业状态独立于任务锁。"""
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

    def submit(self, payload: Dict[str, Any], handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        request_id = str(payload.get("request_id", ""))
        with self._lock:
            if self._current is not None:
                if request_id and self._current["request_id"] == request_id:
                    return self._public_locked(self._current)
                if self._current["state"] == "running":
                    raise RuntimeError("另一规划分派作业仍在执行，请先核对其结果；未再次下发任务")
            job = {
                "job_id": uuid.uuid4().hex,
                "request_id": request_id,
                "state": "running",
                "started_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._current = job
        worker = threading.Thread(
            target=self._run, args=(job, copy.deepcopy(payload), handler),
            name="competition-plan-dispatch", daemon=True,
        )
        worker.start()
        return self.get(job["job_id"])

    def _run(self, job: Dict[str, Any], payload: Dict[str, Any], handler: Callable) -> None:
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
            with self._lock:
                job["state"] = "failed"
                job["error"] = str(detail if detail is not None else error)
                job["finished_at"] = time.time()
        else:
            with self._lock:
                job["state"] = "succeeded"
                job["result"] = result
                job["finished_at"] = time.time()

    def _public_locked(self, job: Dict[str, Any]) -> Dict[str, Any]:
        return dict(job)

    def get(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._current is None or (job_id is not None and self._current["job_id"] != job_id):
                return None
            return self._public_locked(self._current)
