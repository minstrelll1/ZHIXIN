"""规划作业在浏览器断线、重复点击和局部网络卡顿时不会重复下发。"""
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from competition_backend.api import create_app
from competition_backend.plan_jobs import PlanJobRegistry
from competition_backend.tcp_adapter import _ClientConnection


class PlanJobTest(unittest.TestCase):
    def test_new_request_waits_for_running_job_and_keeps_both_results(self):
        registry = PlanJobRegistry()
        started, release = threading.Event(), threading.Event()
        calls = []

        def handle(payload):
            calls.append(payload)
            started.set()
            self.assertTrue(release.wait(2))
            return {"mission": {"mission_id": "m1"}}

        first = registry.submit({"request_id": "click-1"}, handle)
        self.assertTrue(started.wait(1))
        second = registry.submit({"request_id": "click-1"}, handle)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(calls), 1)
        queued = registry.submit({"request_id": "click-elsewhere"}, handle)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(registry.get()["job_id"], queued["job_id"])
        self.assertEqual(len(calls), 1)
        release.set()
        deadline = time.monotonic() + 2
        while registry.get(queued["job_id"])["state"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(registry.get(first["job_id"])["result"]["mission"]["mission_id"], "m1")
        self.assertEqual(registry.get(queued["job_id"])["state"], "succeeded")
        self.assertEqual(len(calls), 2)

    def test_only_latest_queued_request_runs(self):
        registry = PlanJobRegistry()
        started, release = threading.Event(), threading.Event()
        calls = []

        def handle(payload):
            calls.append(payload["request_id"])
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(2))
            return {"mission": {"mission_id": payload["request_id"]}}

        first = registry.submit({"request_id": "first"}, handle)
        self.assertTrue(started.wait(1))
        middle = registry.submit({"request_id": "middle"}, handle)
        latest = registry.submit({"request_id": "latest"}, handle)
        self.assertEqual(registry.get(middle["job_id"])["state"], "superseded")
        self.assertEqual(registry.submit({"request_id": "latest"}, handle)["job_id"], latest["job_id"])
        release.set()
        deadline = time.monotonic() + 2
        while registry.get(latest["job_id"])["state"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(calls, ["first", "latest"])
        self.assertEqual(registry.get(first["job_id"])["state"], "succeeded")

    def test_replacement_runs_after_first_plan_fails(self):
        registry = PlanJobRegistry()
        started, release = threading.Event(), threading.Event()

        def handle(payload):
            if payload["request_id"] == "rejected":
                started.set()
                self.assertTrue(release.wait(2))
                raise ValueError("机载任务被拒绝")
            return {"mission": {"mission_id": "replacement"}}

        first = registry.submit({"request_id": "rejected"}, handle)
        self.assertTrue(started.wait(1))
        replacement = registry.submit({"request_id": "replacement"}, handle)
        release.set()
        deadline = time.monotonic() + 2
        while registry.get(replacement["job_id"])["state"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(registry.get(first["job_id"])["state"], "failed")
        self.assertEqual(registry.get(replacement["job_id"])["result"]["mission"]["mission_id"], "replacement")

    def test_api_returns_job_before_plan_finishes_then_exposes_result(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {
                "COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": data}, clear=False):
            app = create_app()
            client = TestClient(app)
            first = client.post("/api/v1/plan/jobs", json={"subject": "subject1", "request_id": "click-2"})
            self.assertEqual(first.status_code, 200, first.text)
            job_id = first.json()["job_id"]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                current = client.get("/api/v1/plan/jobs/" + job_id)
                self.assertEqual(current.status_code, 200)
                if current.json()["state"] != "running":
                    break
                time.sleep(.02)
            self.assertEqual(current.json()["state"], "succeeded", current.text)
            self.assertEqual(current.json()["result"]["mission"]["mission_id"],
                             client.get("/api/v1/status").json()["mission"]["mission_id"])
            self.assertEqual(client.get("/api/v1/plan/jobs/current").json()["job_id"], job_id)

    def test_rejected_plan_has_explicit_failed_job_and_no_assignment(self):
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {
                "COMPETITION_ADAPTER": "sim", "COMPETITION_DATA_DIR": data}, clear=False):
            app = create_app()
            client = TestClient(app)
            created = client.post("/api/v1/plan/jobs", json={"subject": "unknown", "request_id": "bad"})
            self.assertEqual(created.status_code, 200)
            job_id = created.json()["job_id"]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                job = client.get("/api/v1/plan/jobs/" + job_id).json()
                if job["state"] != "running":
                    break
                time.sleep(.01)
            self.assertEqual(job["state"], "failed")
            self.assertIn("unknown subject", job["error"])
            self.assertIsNone(client.get("/api/v1/status").json()["mission"])

    def test_stalled_onboard_send_is_bounded_and_socket_is_closed(self):
        class StalledSocket:
            def __init__(self):
                self.closed = threading.Event()

            def sendall(self, _data):
                self.closed.wait(2)
                raise OSError("closed")

            def shutdown(self, _how):
                self.closed.set()

            def close(self):
                self.closed.set()

        fake = StalledSocket()
        client = _ClientConnection(fake, ("127.0.0.1", 1))
        with patch("competition_backend.tcp_adapter.COMMAND_SEND_TIMEOUT_SECONDS", .03):
            start = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "发送超过"):
                client.send({"type": "assign_task"})
            self.assertLess(time.monotonic() - start, .5)
            self.assertTrue(fake.closed.is_set())


if __name__ == "__main__":
    unittest.main()
