# -*- coding: utf-8 -*-
"""Реестр типов и планировщик: полосы, приоритет, отмена, рестарт.

Прогон: cd backend && python manage.py test vc_jobs.tests_scheduler
"""

import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from vc_jobs.model import Job
from vc_jobs.registry import JobRegistry, JobType, UnknownLane
from vc_jobs.scheduler import (
    JobCancelled,
    JobConflict,
    JobCycleError,
    JobInterrupted,
    JobSpec,
    LaneCapacity,
    Scheduler,
)
from vc_jobs.store import JobStore

LANES = {"export", "archive", "import", "system"}


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class Gate:
    """Исполнитель, который стоит, пока тест не отпустит."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, job, ctx):
        self.calls += 1
        self.started.set()
        self.release.wait(5)
        return {"summary": "готово"}


class RegistryTests(SimpleTestCase):
    def _type(self, key="test.echo", lane="export", **kw):
        executor = kw.pop("executor", lambda job, ctx: None)
        return JobType(key=key, lane=lane, label="Тест",
                       executor=executor, **kw)

    def test_register_and_get(self):
        reg = JobRegistry()
        jt = self._type()
        reg.register(jt)

        self.assertIs(reg.get("test.echo"), jt)
        self.assertEqual(reg.types(), [jt])

    def test_unknown_type_returns_none(self):
        self.assertIsNone(JobRegistry().get("nope"))

    def test_duplicate_key_rejected(self):
        reg = JobRegistry()
        reg.register(self._type())

        with self.assertRaises(ValueError):
            reg.register(self._type())

    def test_unknown_lane_rejected(self):
        with self.assertRaises(UnknownLane):
            self._type(lane="gpu")

    def test_executor_must_be_callable(self):
        with self.assertRaises(ValueError):
            self._type(executor=None)

    def test_manifest_is_json_ready_and_has_no_executor(self):
        reg = JobRegistry()
        reg.register(self._type(cancellable=False, resumable=False,
                                result_kind="file", retention_days=7.0))

        manifest = reg.manifest()

        self.assertEqual(manifest[0]["key"], "test.echo")
        self.assertEqual(manifest[0]["cancellable"], False)
        self.assertEqual(manifest[0]["retention_days"], 7.0)
        self.assertNotIn("executor", manifest[0])


class SchedulerTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcsched_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = JobStore(self.root)
        self.reg = JobRegistry()
        self.schedulers = []

    def tearDown(self):
        for scheduler in self.schedulers:
            scheduler.stop()

    def make(self, key="test.echo", lane="export", executor=None, **kw):
        jt = JobType(key=key, lane=lane, label=key,
                     executor=executor or (lambda job, ctx: None), **kw)
        self.reg.register(jt)
        return jt

    def scheduler(self, **kw):
        caps = {"export": 1, "archive": 1, "import": 1, "system": 1}
        caps.update(kw.pop("lane_capacities", {}))
        s = Scheduler(self.store, self.reg, lane_capacities=caps,
                      max_workers=kw.pop("max_workers", 4),
                      progress_interval=kw.pop("progress_interval", 0.0),
                      **kw)
        self.schedulers.append(s)
        s.start()
        return s

    def submit(self, s, **kw):
        data = dict(type="test.echo",
                    target={"kind": "task", "id": "t1"}, owner="local")
        data.update(kw)
        return s.submit(JobSpec(**data))


class SubmitTests(SchedulerTestBase):
    def test_submit_persists_queued_job_and_returns_it(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()

        running, _ = self.submit(s, target={"kind": "task", "id": "run"})
        self.assertTrue(gate.started.wait(2))
        job, dedup = self.submit(s, target={"kind": "task", "id": "wait"})

        self.assertFalse(dedup)
        self.assertEqual(job.status, "queued")
        self.assertEqual(self.store.get(job.id).status, "queued")
        gate.release.set()
        self.assertTrue(wait_until(lambda: s.get(running.id).status == "done"))
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))
        self.assertEqual(self.store.get(job.id).status, "done")

    def test_repeated_active_request_is_deduplicated(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()

        first, _ = self.submit(s)
        self.assertTrue(gate.started.wait(2))
        second, dedup = self.submit(s)

        self.assertTrue(dedup)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(s.list()[0]), 1)
        gate.release.set()

    def test_different_owners_are_not_deduplicated(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()

        first, _ = self.submit(s, owner="alice")
        self.assertTrue(gate.started.wait(2))
        second, dedup = self.submit(s, owner="bob")

        self.assertFalse(dedup)
        self.assertNotEqual(first.id, second.id)
        gate.release.set()

    def test_idempotency_key_returns_terminal_job(self):
        self.make()
        s = self.scheduler()
        first, _ = self.submit(s, idempotency_key="abc")
        self.assertTrue(wait_until(lambda: s.get(first.id).status == "done"))

        second, dedup = self.submit(s, idempotency_key="abc")

        self.assertTrue(dedup)
        self.assertEqual(second.id, first.id)

    def test_unknown_type_rejected(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(KeyError):
            self.submit(s, type="nope")

    def test_invalid_target_rejected(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(ValueError):
            self.submit(s, target={"kind": "task"})


class ExecutionTests(SchedulerTestBase):
    def test_progress_is_visible_and_persisted(self):
        def run(job, ctx):
            ctx.progress(2, 5, "режу", unit="fragment")
            return None
        self.make(executor=run)
        s = self.scheduler()

        job, _ = self.submit(s)

        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))
        saved = self.store.get(job.id)
        self.assertEqual(saved.progress["current"], 2)
        self.assertEqual(saved.progress["total"], 5)
        self.assertEqual(saved.progress["unit"], "fragment")

    def test_result_is_stored(self):
        self.make(executor=lambda job, ctx: {
            "summary": "33 нарезки", "files": ["a.mp4"]})
        s = self.scheduler()

        job, _ = self.submit(s)

        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))
        self.assertEqual(s.get(job.id).result["summary"], "33 нарезки")
        self.assertEqual(s.get(job.id).result["files"], ["a.mp4"])

    def test_executor_error_becomes_error_status(self):
        def boom(job, ctx):
            raise RuntimeError("диск кончился")
        self.make(executor=boom)
        s = self.scheduler()

        job, _ = self.submit(s)

        self.assertTrue(wait_until(lambda: s.get(job.id).status == "error"))
        self.assertEqual(s.get(job.id).error["message"], "диск кончился")
        self.assertEqual(s.get(job.id).error["code"], "executor_error")

    def test_start_sets_timestamps(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()

        job, _ = self.submit(s)
        self.assertTrue(gate.started.wait(2))
        running = s.get(job.id)

        self.assertEqual(running.status, "running")
        self.assertTrue(running.started_at)
        gate.release.set()

    def test_lane_capacity_is_not_exceeded(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler(lane_capacities={"export": 1})

        first, _ = self.submit(s, target={"kind": "task", "id": "t1"})
        second, _ = self.submit(s, target={"kind": "task", "id": "t2"})
        self.assertTrue(gate.started.wait(2))
        time.sleep(0.05)

        self.assertEqual(s.get(first.id).status, "running")
        self.assertEqual(s.get(second.id).status, "queued")
        gate.release.set()
        self.assertTrue(wait_until(lambda: s.get(second.id).status == "done"))

    def test_global_capacity_limits_all_lanes(self):
        gate = Gate()
        self.make(key="a", lane="export", executor=gate)
        self.make(key="b", lane="archive", executor=gate)
        s = self.scheduler(max_workers=1)

        first, _ = self.submit(s, type="a", target={"kind": "task", "id": "t1"})
        second, _ = self.submit(s, type="b", target={"kind": "task", "id": "t2"})
        self.assertTrue(gate.started.wait(2))
        time.sleep(0.05)

        self.assertEqual(s.get(first.id).status, "running")
        self.assertEqual(s.get(second.id).status, "queued")
        gate.release.set()

    def test_priority_starts_first(self):
        order = []
        gate = Gate()

        def run(job, ctx):
            order.append(job.target["id"])
            gate.release.wait(5)
            return None

        self.make(executor=run)
        s = self.scheduler()
        # Занимаем слот, чтобы обе заявки легли в очередь.
        self.submit(s, target={"kind": "task", "id": "block"})
        self.assertTrue(wait_until(lambda: order == ["block"]))
        self.submit(s, target={"kind": "task", "id": "low"}, priority=100)
        self.submit(s, target={"kind": "task", "id": "high"}, priority=0)
        # Первый исполнитель тоже ждёт release, поэтому отпускаем после
        # постановки обеих заявок.
        gate.release.set()

        self.assertTrue(wait_until(lambda: len(order) == 3))
        self.assertEqual(order, ["block", "high", "low"])

    def test_fifo_within_same_priority(self):
        order = []
        release = threading.Event()

        def run(job, ctx):
            order.append(job.target["id"])
            if job.target["id"] == "block":
                release.wait(5)
            return None

        self.make(executor=run)
        s = self.scheduler()
        self.submit(s, target={"kind": "task", "id": "block"})
        self.assertTrue(wait_until(lambda: order == ["block"]))
        self.submit(s, target={"kind": "task", "id": "first"})
        self.submit(s, target={"kind": "task", "id": "second"})
        release.set()

        self.assertTrue(wait_until(lambda: len(order) == 3))
        self.assertEqual(order, ["block", "first", "second"])


class CancelTests(SchedulerTestBase):
    def test_cancel_queued_job_immediately(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()
        self.submit(s, target={"kind": "task", "id": "block"})
        self.assertTrue(gate.started.wait(2))
        queued, _ = self.submit(s, target={"kind": "task", "id": "q"})

        cancelled = s.cancel(queued.id)

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.store.get(queued.id).status, "cancelled")
        gate.release.set()

    def test_cancel_running_is_cooperative(self):
        def run(job, ctx):
            for _ in range(200):
                ctx.checkpoint()
                time.sleep(0.01)
            return None
        self.make(executor=run)
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "running"))

        requested = s.cancel(job.id)

        self.assertTrue(requested.cancel_requested)
        self.assertEqual(requested.display_status, "cancelling")
        self.assertTrue(wait_until(
            lambda: s.get(job.id).status == "cancelled"))

    def test_cancel_terminal_conflicts(self):
        self.make()
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))

        with self.assertRaises(JobConflict):
            s.cancel(job.id)

    def test_cancel_unknown_is_not_found(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(KeyError):
            s.cancel("missing")

    def test_cancel_queued_prerequisite_fails_dependent(self):
        gate = Gate()
        self.make(key="block", lane="export", executor=gate)
        self.make(key="parent", lane="export", executor=lambda j, c: None)
        self.make(key="child", lane="export", executor=lambda j, c: None)
        s = self.scheduler(max_workers=1)
        self.submit(s, type="block", target={"kind": "task", "id": "block"})
        self.assertTrue(gate.started.wait(2))
        parent, _ = self.submit(s, type="parent",
                                target={"kind": "task", "id": "p"})
        child, _ = self.submit(s, type="child",
                               target={"kind": "task", "id": "c"},
                               depends_on=[parent.id])

        s.cancel(parent.id)
        gate.release.set()

        self.assertEqual(s.get(parent.id).status, "cancelled")
        self.assertEqual(s.get(child.id).status, "error")
        self.assertEqual(s.get(child.id).error["code"],
                         "prerequisite_cancelled")


class ResumeTests(SchedulerTestBase):
    def _stored(self, **kw):
        data = dict(
            id="a" * 36, type="test.echo", lane="export", status="running",
            dedupe_key="k", owner="local",
            target={"kind": "task", "id": "t1"},
            created_at=(datetime.now(timezone.utc)
                        - timedelta(minutes=5)).isoformat(),
        )
        data.update(kw)
        return Job(**data)

    def test_running_resumable_becomes_queued_with_new_attempt(self):
        done = threading.Event()
        self.make(executor=lambda job, ctx: done.set())
        self.store.save(self._stored(attempt=1))

        s = self.scheduler()

        self.assertTrue(done.wait(3))
        job = s.get("a" * 36)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.attempt, 2)

    def test_running_non_resumable_stays_interrupted(self):
        self.make(resumable=False)
        self.store.save(self._stored())

        s = self.scheduler()

        self.assertEqual(s.get("a" * 36).status, "interrupted")

    def test_manually_cancelled_is_never_resumed(self):
        self.make()
        self.store.save(self._stored(cancel_requested=True))

        s = self.scheduler()

        self.assertEqual(s.get("a" * 36).status, "cancelled")

    def test_queued_job_resumes_in_place(self):
        done = threading.Event()
        self.make(executor=lambda job, ctx: done.set())
        self.store.save(self._stored(status="queued", started_at=None))

        s = self.scheduler()

        self.assertTrue(done.wait(3))
        self.assertEqual(s.get("a" * 36).status, "done")


class DependencyTests(SchedulerTestBase):
    def test_job_waits_for_local_dependency(self):
        gate = Gate()
        self.make(key="parent", lane="system", executor=gate)
        self.make(key="child", lane="export",
                  executor=lambda j, c: None)
        s = self.scheduler()
        parent, _ = self.submit(s, type="parent",
                                target={"kind": "task", "id": "t1"})
        self.assertTrue(gate.started.wait(2))
        child, _ = self.submit(s, type="child",
                               target={"kind": "task", "id": "t1"},
                               depends_on=[parent.id])
        time.sleep(0.05)

        self.assertEqual(s.get(child.id).status, "queued")
        gate.release.set()
        self.assertTrue(wait_until(lambda: s.get(child.id).status == "done"))

    def test_unknown_dependency_blocks_forever(self):
        self.make()
        s = self.scheduler()
        job, _ = self.submit(s, depends_on=["external"])

        time.sleep(0.1)

        self.assertEqual(s.get(job.id).status, "queued")

    def test_prerequisite_is_materialized_and_ordered(self):
        gate = Gate()
        self.make(key="parent", lane="system", executor=gate)
        self.make(key="child", lane="export",
                  executor=lambda j, c: None,
                  prerequisites=lambda spec: [
                      JobSpec(type="parent", target=spec.target,
                              owner=spec.owner)])
        s = self.scheduler()
        child, _ = self.submit(s, type="child")
        self.assertTrue(gate.started.wait(2))

        parent_id = s.get(child.id).depends_on[0]
        self.assertEqual(s.get(parent_id).status, "running")
        self.assertEqual(s.get(child.id).status, "queued")
        gate.release.set()
        self.assertTrue(wait_until(lambda: s.get(child.id).status == "done"))

    def test_prerequisite_cycle_rejected(self):
        self.make(key="a", prerequisites=lambda spec: [JobSpec(
            type="b", target=spec.target, owner=spec.owner)])
        self.make(key="b", prerequisites=lambda spec: [JobSpec(
            type="a", target=spec.target, owner=spec.owner)])
        s = self.scheduler()

        with self.assertRaises(JobCycleError):
            self.submit(s, type="a")

    def test_prerequisite_depth_is_limited(self):
        def loop(key):
            return lambda spec: [JobSpec(type=key, target=spec.target,
                                         owner=spec.owner)]
        self.make(key="a", prerequisites=loop("b"))
        self.make(key="b", prerequisites=loop("a"))
        s = self.scheduler()

        with self.assertRaises(JobCycleError):
            self.submit(s, type="a")


class StopTests(SchedulerTestBase):
    def test_stop_marks_running_interrupted(self):
        def run(job, ctx):
            while True:
                ctx.checkpoint()
                time.sleep(0.01)
        self.make(executor=run)
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "running"))

        s.stop()

        self.assertEqual(s.get(job.id).status, "interrupted")
        self.assertEqual(self.store.get(job.id).status, "interrupted")
        # Второй stop безопасен.
        s.stop()

    def test_interrupt_exception_keeps_interrupted(self):
        started = threading.Event()

        def run(job, ctx):
            started.set()
            raise JobInterrupted()
        self.make(executor=run)
        s = self.scheduler()
        job, _ = self.submit(s)

        self.assertTrue(started.wait(2))
        self.assertTrue(wait_until(
            lambda: s.get(job.id).status == "interrupted"))


class ListSummaryTests(SchedulerTestBase):
    def test_filters_and_cursor(self):
        self.make()
        s = self.scheduler()
        for i in range(3):
            self.submit(s, target={"kind": "task", "id": f"t{i}"})
        self.assertTrue(wait_until(lambda: len(
            [j for j in s.list()[0] if j.status == "done"]) == 3))

        jobs, cursor = s.list(limit=2)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(cursor)
        rest, cursor2 = s.list(limit=2, cursor=cursor)
        self.assertEqual(len(rest), 1)
        self.assertFalse(cursor2)

        only_t1, _ = s.list(target="task:t1")
        self.assertEqual(len(only_t1), 1)
        self.assertEqual(only_t1[0].target["id"], "t1")

    def test_summary_counts_active(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()
        self.submit(s)
        self.assertTrue(gate.started.wait(2))
        self.submit(s, target={"kind": "task", "id": "q"})

        summary = s.summary()

        self.assertEqual(summary["running"], 1)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["active"], 2)
        gate.release.set()

    def test_retry_creates_new_attempt(self):
        calls = []

        def run(job, ctx):
            calls.append(job.attempt)
            if job.attempt == 1:
                raise RuntimeError("сбой")
        self.make(executor=run)
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "error"))

        retried = s.retry(job.id)

        self.assertNotEqual(retried.id, job.id)
        self.assertEqual(retried.attempt, 2)
        self.assertTrue(wait_until(
            lambda: s.get(retried.id).status == "done"))
        self.assertEqual(calls, [1, 2])

    def test_retry_active_conflicts(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(gate.started.wait(2))

        with self.assertRaises(JobConflict):
            s.retry(job.id)
        gate.release.set()
