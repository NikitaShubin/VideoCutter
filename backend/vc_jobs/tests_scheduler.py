# -*- coding: utf-8 -*-
"""Реестр типов и планировщик: полосы, приоритет, отмена, рестарт.

Прогон: cd backend && python manage.py test vc_jobs.tests_scheduler
"""

import base64
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs.model import Job
from vc_jobs.registry import JobRegistry, JobType, UnknownLane
from vc_jobs.scheduler import (
    JobCancelled,
    JobConflict,
    JobCycleError,
    JobInterrupted,
    JobResult,
    JobSpec,
    LaneCapacity,
    Scheduler,
    _env_float,
    _env_int,
    build_scheduler,
    default_lane_capacities,
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

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            self._type(key="")

    def test_prerequisites_must_be_callable(self):
        with self.assertRaises(ValueError):
            self._type(prerequisites="not-callable")

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

    def test_start_is_idempotent(self):
        self.make()
        s = self.scheduler()

        self.assertIs(s.start(), s)
        self.assertEqual(s.summary()["queued"] + s.summary()["done"], 0)

    def test_get_starts_scheduler_lazily(self):
        self.make()
        s = Scheduler(self.store, self.reg,
                      lane_capacities={"export": 1, "archive": 1,
                                       "import": 1, "system": 1},
                      max_workers=1, progress_interval=0.0)
        self.schedulers.append(s)

        self.assertIsNone(s.get("missing"))
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))


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

    def test_unregistered_type_at_execution_becomes_error(self):
        self.make()
        s = self.scheduler()
        real_get = self.reg.get
        calls = []

        def get(key):
            calls.append(key)
            return real_get(key) if len(calls) == 1 else None

        with mock.patch.object(self.reg, "get", side_effect=get):
            job, _ = self.submit(s)
            self.assertTrue(wait_until(
                lambda: s.get(job.id).status == "error"))

        self.assertIn("тип не зарегистрирован",
                      s.get(job.id).error["message"])

    def test_scalar_result_is_normalized_to_summary(self):
        self.make(executor=lambda job, ctx: 42)
        s = self.scheduler()

        job, _ = self.submit(s)

        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))
        self.assertEqual(s.get(job.id).result["summary"], "42")

    def test_progress_for_unknown_job_is_ignored(self):
        self.make()
        s = self.scheduler()
        job = Job(id="gone", type="test.echo", lane="export",
                  status="queued", dedupe_key="k", owner="local",
                  target={"kind": "task", "id": "t1"})

        s.progress(job, 1, 2)

        self.assertIsNone(s.get("gone"))

    def test_interrupted_status_is_sticky(self):
        self.make()
        s = self.scheduler()
        job, _ = self.submit(s)
        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))
        job.status = "interrupted"

        s._finish(job, "done", result=JobResult(summary="поздно"),
                  job_type=self.reg.get("test.echo"))

        self.assertEqual(job.status, "interrupted")

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

    def _dependent(self, prereq_id, **kw):
        data = dict(
            id="b" * 36, type="test.echo", lane="export", status="queued",
            dedupe_key="dep", owner="local",
            target={"kind": "task", "id": "t2"},
            depends_on=[prereq_id],
            created_at=(datetime.now(timezone.utc)
                        - timedelta(minutes=4)).isoformat(),
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

    def test_unknown_type_from_disk_becomes_error(self):
        self.store.save(self._stored(type="ghost.type", status="queued"))

        s = self.scheduler()

        job = s.get("a" * 36)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error["code"], "unknown_type")
        self.assertEqual(self.store.get("a" * 36).status, "error")

    def test_restart_of_unknown_prerequisite_fails_dependent(self):
        self.store.save(self._stored(type="ghost.type", status="queued"))
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()

        dependent = s.get("b" * 36)
        self.assertEqual(dependent.status, "error")
        self.assertEqual(dependent.error["code"], "prerequisite_failed")

    def test_restart_of_cancelled_prerequisite_fails_dependent(self):
        self.make()
        self.store.save(self._stored(cancel_requested=True))
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()

        dependent = s.get("b" * 36)
        self.assertEqual(dependent.status, "error")
        self.assertEqual(dependent.error["code"],
                         "prerequisite_cancelled")

    def test_restart_of_interrupted_non_resumable_fails_dependent(self):
        self.make(resumable=False)
        self.store.save(self._stored())
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()

        dependent = s.get("b" * 36)
        self.assertEqual(dependent.status, "error")
        self.assertEqual(dependent.error["code"], "prerequisite_failed")

    def test_restart_of_stopped_resumable_requeues(self):
        done = threading.Event()
        self.make(executor=lambda job, ctx: done.set())
        self.store.save(self._stored(status="interrupted", attempt=2))

        s = self.scheduler()

        self.assertTrue(done.wait(3))
        job = s.get("a" * 36)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.attempt, 3)

    def test_restart_does_not_resume_job_cancelled_before_stop(self):
        calls = []
        self.make(executor=lambda job, ctx: calls.append(job.id))
        self.store.save(self._stored(
            status="interrupted", cancel_requested=True))

        s = self.scheduler()

        self.assertEqual(s.get("a" * 36).status, "cancelled")
        self.assertEqual(calls, [])

    def test_restart_of_cancelled_interrupted_fails_dependent(self):
        self.make()
        self.store.save(self._stored(
            status="interrupted", cancel_requested=True))
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()

        dependent = s.get("b" * 36)
        self.assertEqual(dependent.status, "error")
        self.assertEqual(dependent.error["code"],
                         "prerequisite_cancelled")

    def test_restart_of_stopped_non_resumable_fails_dependent(self):
        self.make(resumable=False)
        self.store.save(self._stored(status="interrupted"))
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()

        dependent = s.get("b" * 36)
        self.assertEqual(dependent.status, "error")
        self.assertEqual(dependent.error["code"], "prerequisite_failed")

    def test_restart_of_resumable_keeps_dependent_queued(self):
        gate = Gate()
        self.make(executor=gate)
        self.store.save(self._stored())
        self.store.save(self._dependent("a" * 36))

        s = self.scheduler()
        self.assertTrue(gate.started.wait(2))

        self.assertEqual(s.get("b" * 36).status, "queued")
        gate.release.set()
        self.assertTrue(wait_until(
            lambda: s.get("b" * 36).status == "done"))


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

    def test_failed_prerequisite_fails_dependent(self):
        def boom(job, ctx):
            raise RuntimeError("сломалось")
        self.make(key="parent", lane="system", executor=boom)
        self.make(key="child", lane="export",
                  executor=lambda j, c: None)
        s = self.scheduler()
        parent, _ = self.submit(s, type="parent",
                                target={"kind": "task", "id": "t1"})
        child, _ = self.submit(s, type="child",
                               target={"kind": "task", "id": "t1"},
                               depends_on=[parent.id])

        self.assertTrue(wait_until(lambda: s.get(child.id).status == "error"))

        self.assertEqual(s.get(child.id).error["code"],
                         "prerequisite_failed")
        self.assertIn("сломалось", s.get(child.id).error["message"])

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

    def test_existing_prerequisite_priority_is_promoted(self):
        gate = Gate()
        self.make(key="blocker", lane="system", executor=gate)
        self.make(key="parent", lane="system",
                  executor=lambda job, ctx: None)
        self.make(key="child", lane="export",
                  executor=lambda job, ctx: None,
                  prerequisites=lambda spec: [
                      JobSpec(type="parent", target=spec.target,
                              owner=spec.owner, inherit_priority=True)])
        s = self.scheduler()

        self.submit(s, type="blocker", target={"kind": "task", "id": "b"})
        self.assertTrue(gate.started.wait(2))
        parent, _ = self.submit(
            s, type="parent", target={"kind": "task", "id": "t1"},
            priority=100)
        self.assertEqual(s.get(parent.id).status, "queued")

        self.submit(s, type="child", target={"kind": "task", "id": "t1"},
                    priority=0)

        self.assertEqual(s.get(parent.id).priority, 0)
        gate.release.set()

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

    def test_too_deep_prerequisite_chain_rejected(self):
        def next_type(key):
            return lambda spec: [JobSpec(type=key, target=spec.target,
                                         owner=spec.owner)]
        self.make(key="a", prerequisites=next_type("b"))
        self.make(key="b", prerequisites=next_type("c"))
        self.make(key="c", prerequisites=next_type("d"))
        self.make(key="d", prerequisites=next_type("e"))
        self.make(key="e")
        s = self.scheduler()

        with self.assertRaises(JobCycleError) as caught:
            self.submit(s, type="a")

        self.assertIn("глубокая", str(caught.exception))

    def test_prerequisite_must_return_job_spec(self):
        self.make(key="child", prerequisites=lambda spec: ["not-a-spec"])
        s = self.scheduler()

        with self.assertRaises(ValueError):
            self.submit(s, type="child")

    def test_unknown_prerequisite_type_rejected(self):
        self.make(key="child", prerequisites=lambda spec: [
            JobSpec(type="ghost", target=spec.target, owner=spec.owner)])
        s = self.scheduler()

        with self.assertRaises(KeyError):
            self.submit(s, type="child")


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

    def test_interrupt_of_non_resumable_fails_dependent(self):
        self.make(key="parent", resumable=False,
                  executor=lambda job, ctx: (_ for _ in ()).throw(
                      JobInterrupted()))
        self.make(key="child")
        s = self.scheduler()
        parent, _ = self.submit(
            s, type="parent", target={"kind": "task", "id": "t1"})
        child, _ = self.submit(
            s, type="child", target={"kind": "task", "id": "t2"},
            depends_on=[parent.id])

        self.assertTrue(wait_until(lambda: s.get(child.id).status == "error"))
        self.assertEqual(s.get(child.id).error["code"],
                         "prerequisite_failed")


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

    def test_retry_unknown_is_not_found(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(KeyError):
            s.retry("missing")

    def test_set_retention_unknown_is_not_found(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(KeyError):
            s.set_retention("missing", "2030-01-01T00:00:00+00:00")

    def test_bad_cursor_rejected(self):
        self.make()
        s = self.scheduler()

        with self.assertRaises(ValueError) as caught:
            s.list(cursor="%%%")

        self.assertIn("курсор", str(caught.exception))

    def test_non_utf8_cursor_rejected(self):
        self.make()
        s = self.scheduler()
        cursor = base64.urlsafe_b64encode(b"\xff").decode("ascii")

        with self.assertRaises(ValueError) as caught:
            s.list(cursor=cursor)

        self.assertIn("курсор", str(caught.exception))

    def test_list_filters_by_type_status_and_project(self):
        gate = Gate()
        self.make(executor=gate)
        s = self.scheduler()
        running, _ = self.submit(
            s, target={"kind": "task", "id": "t1", "project_id": "p1"})
        self.assertTrue(gate.started.wait(2))
        queued, _ = self.submit(
            s, target={"kind": "task", "id": "t2", "project_id": "p2"})

        s.cancel(running.id)
        cancelling, _ = s.list(status="cancelling")
        by_type, _ = s.list(type="test.echo")
        by_project, _ = s.list(project_id="p2")
        gate.release.set()

        self.assertEqual([job.id for job in cancelling], [running.id])
        self.assertEqual({job.id for job in by_type},
                         {running.id, queued.id})
        self.assertEqual([job.id for job in by_project], [queued.id])

    def test_activity_failure_does_not_break_job(self):
        activity = mock.Mock()
        activity.append.side_effect = OSError("журнал недоступен")
        self.make()
        s = self.scheduler(activity=activity)

        job, _ = self.submit(s)

        self.assertTrue(wait_until(lambda: s.get(job.id).status == "done"))


class RetentionTests(SchedulerTestBase):
    def setUp(self):
        super().setUp()
        self.make()

    def _done(self, scheduler, **kw):
        job, _ = self.submit(scheduler, **kw)
        self.assertTrue(wait_until(
            lambda: scheduler.get(job.id).status == "done"))
        return scheduler.get(job.id)

    def _age(self, job, days):
        job.finished_at = (datetime.now(timezone.utc)
                           - timedelta(days=days)).isoformat()
        self.store.save(job)

    def test_old_done_job_is_pruned_from_memory_and_disk(self):
        scheduler = self.scheduler()
        job = self._done(scheduler)
        self._age(job, 31)

        removed = scheduler.prune()

        self.assertEqual(removed, 1)
        self.assertIsNone(scheduler.get(job.id))
        self.assertIsNone(self.store.get(job.id))

    def test_recent_terminal_job_survives(self):
        scheduler = self.scheduler()
        job = self._done(scheduler)
        self._age(job, 29)

        self.assertEqual(scheduler.prune(), 0)
        self.assertIsNotNone(scheduler.get(job.id))

    def test_errors_live_longer_than_done(self):
        self.make(key="test.fail",
                  executor=lambda job, ctx: (_ for _ in ()).throw(
                      RuntimeError("сбой")))
        scheduler = self.scheduler()
        job, _ = self.submit(scheduler, type="test.fail")
        self.assertTrue(wait_until(
            lambda: scheduler.get(job.id).status == "error"))
        job = scheduler.get(job.id)
        self._age(job, 40)

        self.assertEqual(scheduler.prune(), 0)

        self._age(job, 100)
        self.assertEqual(scheduler.prune(), 1)

    def test_active_job_is_never_pruned(self):
        gate = Gate()
        self.make(key="test.gate", executor=gate)
        scheduler = self.scheduler()
        job, _ = self.submit(scheduler, type="test.gate")
        self.assertTrue(gate.started.wait(2))
        job.finished_at = (datetime.now(timezone.utc)
                           - timedelta(days=365)).isoformat()

        self.assertEqual(scheduler.prune(), 0)
        self.assertIsNotNone(scheduler.get(job.id))
        gate.release.set()

    def test_terminal_without_finish_time_is_kept(self):
        scheduler = self.scheduler()
        job = self._done(scheduler)
        job.finished_at = None

        self.assertEqual(scheduler.prune(), 0)

    def test_maintenance_sees_remaining_jobs(self):
        seen = []
        scheduler = self.scheduler(maintenance=seen.append)
        old = self._done(scheduler)
        fresh = self._done(scheduler, target={"kind": "task", "id": "t2"})
        self._age(old, 31)

        scheduler.prune()

        remaining = [job.id for job in seen[-1]]
        self.assertIn(fresh.id, remaining)
        self.assertNotIn(old.id, remaining)

    def test_maintenance_failure_does_not_break_prune(self):
        import logging

        def boom(jobs):
            raise RuntimeError("уборка")

        logging.disable(logging.CRITICAL)
        try:
            scheduler = self.scheduler(maintenance=boom)
            job = self._done(scheduler)
            self._age(job, 31)
            removed = scheduler.prune()
        finally:
            logging.disable(logging.NOTSET)

        self.assertEqual(removed, 1)

    def test_start_prunes_old_records_before_resuming(self):
        first = self.scheduler()
        job = self._done(first)
        self._age(job, 31)
        first.stop()

        second = self.scheduler()

        self.assertIsNone(second.get(job.id))
        self.assertIsNone(self.store.get(job.id))


class ConfigTests(SimpleTestCase):
    def test_env_int_parses_and_falls_back(self):
        with mock.patch.dict(os.environ, {"VC_TEST_INT": "5"}):
            self.assertEqual(_env_int("VC_TEST_INT", 1), 5)
        with mock.patch.dict(os.environ, {"VC_TEST_INT": ""}):
            self.assertEqual(_env_int("VC_TEST_INT", 1), 1)
        with mock.patch.dict(os.environ, {"VC_TEST_INT": "abc"}):
            self.assertEqual(_env_int("VC_TEST_INT", 1), 1)
        with mock.patch.dict(os.environ, {"VC_TEST_INT": "0"}):
            self.assertEqual(_env_int("VC_TEST_INT", 1), 1)

    def test_env_float_parses_and_falls_back(self):
        with mock.patch.dict(os.environ, {"VC_TEST_FLOAT": "2.5"}):
            self.assertEqual(_env_float("VC_TEST_FLOAT", 1.0), 2.5)
        with mock.patch.dict(os.environ, {"VC_TEST_FLOAT": ""}):
            self.assertEqual(_env_float("VC_TEST_FLOAT", 1.0), 1.0)
        with mock.patch.dict(os.environ, {"VC_TEST_FLOAT": "abc"}):
            self.assertEqual(_env_float("VC_TEST_FLOAT", 1.0), 1.0)
        with mock.patch.dict(os.environ, {"VC_TEST_FLOAT": "-3"}):
            self.assertEqual(_env_float("VC_TEST_FLOAT", 1.0), 0.0)

    def test_default_lane_capacities_read_env(self):
        with mock.patch("vc_jobs.scheduler.os.cpu_count", return_value=8), \
                mock.patch.dict(os.environ, {
                    "VC_EXPORT_WORKERS": "3",
                    "VC_ARCHIVE_WORKERS": "2",
                    "VC_IMPORT_WORKERS": "5",
                    "VC_INDEX_BG_THREADS": "4",
                }):
            caps = default_lane_capacities()

        self.assertEqual(caps, {"export": 3, "archive": 2,
                                "import": 5, "system": 4})

    def test_build_scheduler_registers_builtin_types(self):
        root = tempfile.mkdtemp(prefix="vcbuild_")
        self.addCleanup(shutil.rmtree, root, True)

        scheduler = build_scheduler(root)
        self.addCleanup(scheduler.stop)

        self.assertIsNotNone(scheduler.registry.get("export.fragments"))
        self.assertIsNotNone(scheduler.registry.get("backup.task"))
        self.assertIsNotNone(scheduler.activity)
