# -*- coding: utf-8 -*-
"""Системные операции: индекс и валидация задачи.

Индекс/валидация — предпосылки тяжёлой работы. Их результат привязан к
отпечатку файлов: пока видео не менялось, повторный экспорт переиспользует
готовую валидацию и не декодирует гигабайты заново.

Прогон: cd backend && python manage.py test vc_jobs.tests_builtin_system
"""

import os
import time
from types import SimpleNamespace
from unittest import mock

import vc_jobs
import workspace
from vc_fragments.tests import WorkspaceApiTestBase
from vc_jobs import checkpoints
from vc_jobs.builtin import register_builtin
from vc_jobs.builtin_export import EXPORT_TYPE
from vc_jobs.builtin_system import (INDEX_TYPE, VALIDATE_TYPE,
                                    _index_prerequisite, _inspect,
                                    ensure_task_ready, execute_index,
                                    execute_validate, task_fingerprint,
                                    validation_prerequisites)
from vc_jobs.registry import JobRegistry
from vc_jobs.scheduler import JobSpec, Scheduler
from vc_jobs.store import JobStore
from vc_pairs import frame_provider


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class SystemJobTestBase(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self._tmpdir, ".state")
        self.env = mock.patch.dict(
            os.environ, {"VC_STATE_ROOT": self.root})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = JobStore(os.path.join(self.root, "jobs"))
        self.registry = JobRegistry()
        register_builtin(self.registry)
        self.scheduler = Scheduler(
            self.store, self.registry,
            lane_capacities={"export": 1, "archive": 1, "import": 1,
                             "system": 1},
            max_workers=2, progress_interval=0.0)
        self.scheduler.start()
        self.addCleanup(self.scheduler.stop)
        self.addCleanup(workspace.clear_all_workspace_errors)

    def submit(self, type, target=None, **kw):
        job, _ = self.scheduler.submit(JobSpec(
            type=type,
            target=target or {"kind": "task", "id": self.ws_id},
            **kw))
        return job

    def wait_status(self, job_id, status, timeout=20.0):
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job_id).status == status, timeout),
            f"job {job_id} not {status}: "
            f"{self.scheduler.get(job_id).error}")
        return self.scheduler.get(job_id)

    def run_job(self, type, target=None, **kw):
        return self.wait_status(
            self.submit(type, target=target, **kw).id, "done")


class IndexJobTests(SystemJobTestBase):
    def test_index_reports_video_metadata(self):
        job = self.run_job(INDEX_TYPE)

        self.assertEqual(job.result["total_frames"], 10)
        self.assertEqual(job.progress["current"], job.progress["total"])
        self.assertIn("preview", job.result["roles"])

    def test_missing_task_fails_job(self):
        job = self.submit(
            INDEX_TYPE, target={"kind": "task", "id": "no-such"})

        failed = self.wait_status(job.id, "error")
        self.assertIn("не найдена", failed.error["message"])

    def test_broken_video_fails_job(self):
        with open(self.video, "wb") as fh:
            fh.write(b"not a video")

        job = self.submit(INDEX_TYPE)
        failed = self.wait_status(job.id, "error")

        self.assertIn("Не удалось прочитать видео", failed.error["message"])


class ValidationJobTests(SystemJobTestBase):
    def test_validation_clears_workspace_error(self):
        workspace.note_workspace_error(self.ws_id, "старая ошибка")

        job = self.run_job(VALIDATE_TYPE)

        self.assertTrue(job.result["valid"])
        self.assertEqual(job.result["total_frames"], 10)
        self.assertIsNone(workspace.workspace_error(self.ws_id))

    def test_validation_records_broken_video(self):
        with open(self.video, "wb") as fh:
            fh.write(b"not a video")

        job = self.submit(VALIDATE_TYPE)
        self.wait_status(job.id, "error")

        self.assertIn("Не удалось прочитать видео",
                      workspace.workspace_error(self.ws_id))

    def test_validation_depends_on_index(self):
        job = self.submit(VALIDATE_TYPE)
        self.wait_status(job.id, "done")

        deps = self.scheduler.get(job.id).depends_on
        self.assertEqual(len(deps), 1)
        self.assertEqual(self.scheduler.get(deps[0]).type, INDEX_TYPE)


class FingerprintTests(SystemJobTestBase):
    def test_fingerprint_changes_with_video_content(self):
        ws = workspace.get_workspace(self.ws_id)
        before = task_fingerprint(ws)

        with open(self.video, "ab") as fh:
            fh.write(b"x")

        os.utime(self.video, None)
        ws = workspace.get_workspace(self.ws_id)
        self.assertNotEqual(task_fingerprint(ws), before)

    def test_export_prerequisite_is_validation(self):
        specs = validation_prerequisites(JobSpec(
            type=EXPORT_TYPE, target={"kind": "task", "id": self.ws_id}))

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].type, VALIDATE_TYPE)
        self.assertEqual(specs[0].owner, "system")
        self.assertTrue(specs[0].idempotency_key)

    def test_validation_prerequisite_inherits_requester_priority(self):
        specs = validation_prerequisites(JobSpec(
            type=EXPORT_TYPE, target={"kind": "task", "id": self.ws_id},
            priority=10))

        self.assertEqual(specs[0].priority, 10)
        self.assertTrue(specs[0].inherit_priority)

    def test_missing_task_has_no_prerequisites(self):
        specs = validation_prerequisites(JobSpec(
            type=EXPORT_TYPE, target={"kind": "task", "id": "no-such"}))

        self.assertEqual(specs, [])

    def test_done_validation_is_reused_for_same_fingerprint(self):
        ws = workspace.get_workspace(self.ws_id)
        key = "validate:" + task_fingerprint(ws)

        first = self.run_job(VALIDATE_TYPE, idempotency_key=key)
        second = self.run_job(VALIDATE_TYPE, idempotency_key=key)

        self.assertEqual(second.id, first.id)


class CheckpointResumeTests(SystemJobTestBase):
    """Внешний сбой не отменяет уже прочитанные роли той же версии."""

    def _ws(self, source="/source.mp4", preview="/preview.mp4"):
        return SimpleNamespace(original=source, visualization=preview)

    def test_external_failure_keeps_completed_role(self):
        ws = self._ws()
        calls = []

        def metadata(path):
            calls.append(path)
            if path == "/preview.mp4":
                raise OSError("диск отвалился")
            return {"total_frames": 10, "fps": 25}

        with mock.patch.object(
                frame_provider, "get_metadata", side_effect=metadata):
            with self.assertRaises(RuntimeError):
                _inspect(self.ws_id, ws, _Ctx())

        saved = checkpoints.load(self.ws_id, task_fingerprint(ws))
        self.assertEqual(set(saved), {"source"})

        calls.clear()

        def recovered(path):
            calls.append(path)
            return {"total_frames": 10, "fps": 25}

        with mock.patch.object(
                frame_provider, "get_metadata", side_effect=recovered):
            data = _inspect(self.ws_id, ws, _Ctx())

        self.assertEqual(calls, ["/preview.mp4"])
        self.assertEqual(data["total_frames"], 10)

    def test_changed_fingerprint_restarts_from_first_role(self):
        ws = self._ws()
        with mock.patch.object(
                frame_provider, "get_metadata",
                side_effect=OSError("сбой")):
            with self.assertRaises(RuntimeError):
                _inspect(self.ws_id, ws, _Ctx())

        calls = []

        def metadata(path):
            calls.append(path)
            return {"total_frames": 10}

        with mock.patch.object(
                frame_provider, "get_metadata", side_effect=metadata):
            _inspect(self.ws_id, self._ws("/other.mp4"), _Ctx())

        self.assertEqual(calls, ["/other.mp4", "/preview.mp4"])

    def test_successful_validation_removes_checkpoint(self):
        ws = self._ws()
        key = task_fingerprint(ws)
        checkpoints.save_role(
            self.ws_id, key, "source", {"total_frames": 10})

        with mock.patch("vc_jobs.builtin_system._task", return_value=ws), \
                mock.patch("vc_jobs.builtin_system.ws_module."
                           "clear_workspace_error"), \
                mock.patch.object(
                    frame_provider, "get_metadata",
                    return_value={"total_frames": 10}):
            execute_validate(
                SimpleNamespace(target={"kind": "task", "id": self.ws_id}),
                _Ctx())

        self.assertEqual(checkpoints.load(self.ws_id, key), {})

    def test_internal_error_removes_checkpoint(self):
        ws = self._ws()
        key = task_fingerprint(ws)
        checkpoints.save_role(
            self.ws_id, key, "source", {"total_frames": 10})

        with mock.patch("vc_jobs.builtin_system._task", return_value=ws), \
                mock.patch("vc_jobs.builtin_system._inspect",
                           side_effect=KeyError("баг")):
            with self.assertRaises(KeyError):
                execute_validate(
                    SimpleNamespace(
                        target={"kind": "task", "id": self.ws_id}), _Ctx())

        self.assertEqual(checkpoints.load(self.ws_id, key), {})

    def test_index_checkpoint_is_reused_by_validation(self):
        self.run_job(INDEX_TYPE)
        ws = workspace.get_workspace(self.ws_id)
        key = task_fingerprint(ws)
        self.assertTrue(checkpoints.load(self.ws_id, key))

        with mock.patch.object(
                frame_provider, "get_metadata",
                side_effect=AssertionError("видео декодировано повторно")):
            job = self.run_job(VALIDATE_TYPE)

        self.assertTrue(job.result["valid"])
        self.assertEqual(checkpoints.load(self.ws_id, key), {})


class OpenTaskTests(SystemJobTestBase):
    def setUp(self):
        super().setUp()
        vc_jobs.set_scheduler(self.scheduler)
        self.addCleanup(vc_jobs.set_scheduler, None)

    def validations(self):
        return [job for job in self.scheduler.all()
                if job.type == VALIDATE_TYPE]

    def test_opening_task_enqueues_validation(self):
        response = self.client.get(self.ws_detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(wait_until(lambda: self.validations()))
        job = self.validations()[0]
        self.wait_status(job.id, "done")
        self.assertTrue(job.result["valid"])

    def test_reopening_reuses_validation(self):
        self.client.get(self.ws_detail_url)
        self.assertTrue(wait_until(lambda: self.validations()))
        first = self.validations()[0]
        self.wait_status(first.id, "done")

        self.client.get(self.ws_detail_url)
        time.sleep(0.1)

        self.assertEqual([job.id for job in self.validations()], [first.id])

    def test_opening_task_survives_validation_submit_failure(self):
        with mock.patch("vc_jobs.builtin_system.ensure_task_ready",
                        side_effect=RuntimeError("очередь недоступна")):
            with self.assertLogs("vc_pairs.views", level="ERROR"):
                response = self.client.get(self.ws_detail_url)

        self.assertEqual(response.status_code, 200)

    def test_ensure_task_ready_skips_missing_task(self):
        self.assertIsNone(ensure_task_ready("no-such"))

    def test_ensure_task_ready_survives_submit_failure(self):
        with mock.patch.object(self.scheduler, "submit",
                               side_effect=RuntimeError("сбой")):
            self.assertIsNone(ensure_task_ready(self.ws_id))


class _Ctx:
    def checkpoint(self):
        pass

    def progress(self, *args, **kwargs):
        pass


class FingerprintEdgeTests(SystemJobTestBase):
    def test_fingerprint_without_video_roles_is_none(self):
        ws = SimpleNamespace(original="", visualization="")

        self.assertEqual(task_fingerprint(ws), "none")

    def test_fingerprint_marks_missing_video(self):
        ws = SimpleNamespace(original="/no/such.mp4", visualization="")

        self.assertNotEqual(task_fingerprint(ws), "none")


class InspectionEdgeTests(SystemJobTestBase):
    def test_inspect_without_video_roles_fails(self):
        ws = SimpleNamespace(original="", visualization="")

        with self.assertRaises(RuntimeError) as caught:
            _inspect(self.ws_id, ws, _Ctx())

        self.assertIn("видео не распознано", str(caught.exception))
        self.assertIn("видео не распознано",
                      workspace.workspace_error(self.ws_id))

    def test_inspect_oserror_is_attributed_to_file(self):
        ws = SimpleNamespace(original=self.video, visualization="")

        with mock.patch.object(frame_provider, "get_metadata",
                               side_effect=OSError("диск")):
            with self.assertRaises(RuntimeError) as caught:
                _inspect(self.ws_id, ws, _Ctx())

        self.assertIn("Не удалось прочитать видео: диск",
                      str(caught.exception))
        self.assertIn("диск", workspace.workspace_error(self.ws_id))

    def test_video_without_frames_is_bad(self):
        ws = SimpleNamespace(original=self.video, visualization="")

        with mock.patch.object(frame_provider, "get_metadata",
                               return_value={"total_frames": 0}):
            with self.assertRaises(RuntimeError) as caught:
                _inspect(self.ws_id, ws, _Ctx())

        self.assertIn("не содержит кадров", str(caught.exception))

    def test_frame_count_mismatch_sets_warning(self):
        ws = SimpleNamespace(original="/source.mp4",
                             visualization="/preview.mp4")

        def metadata(path):
            return {"total_frames": 10 if path == "/source.mp4" else 9,
                    "fps": 10}

        with mock.patch.object(frame_provider, "get_metadata",
                               side_effect=metadata):
            data = _inspect(self.ws_id, ws, _Ctx())

        self.assertIn("не совпадает", data["warning"])
        self.assertIn("10", data["warning"])
        self.assertIn("9", data["warning"])

    def test_index_cancelled_by_task_removal_returns_summary(self):
        job = SimpleNamespace(target={"id": self.ws_id}, params={})

        with mock.patch.object(frame_provider, "get_metadata",
                               side_effect=frame_provider._DecodeCancelled()):
            result = execute_index(job, _Ctx())

        self.assertIn("удалена", result.summary)

    def test_validation_cancelled_by_task_removal_returns_summary(self):
        job = SimpleNamespace(target={"id": self.ws_id}, params={})

        with mock.patch.object(frame_provider, "get_metadata",
                               side_effect=frame_provider._DecodeCancelled()):
            result = execute_validate(job, _Ctx())

        self.assertIn("удалена", result.summary)

    def test_index_requires_task_id(self):
        job = SimpleNamespace(target={"id": ""}, params={})

        with self.assertRaises(RuntimeError) as caught:
            execute_index(job, _Ctx())

        self.assertIn("не указан id", str(caught.exception))


class PrerequisiteEdgeTests(SystemJobTestBase):
    def test_index_prerequisite_skips_missing_task(self):
        specs = _index_prerequisite(JobSpec(
            type=VALIDATE_TYPE, target={"kind": "task", "id": "no-such"}))

        self.assertEqual(specs, [])

    def test_validation_prerequisites_skip_non_video_consumers(self):
        specs = validation_prerequisites(JobSpec(
            type=INDEX_TYPE, target={"kind": "task", "id": self.ws_id}))

        self.assertEqual(specs, [])

    def test_validation_prerequisites_require_task_target(self):
        specs = validation_prerequisites(JobSpec(
            type=EXPORT_TYPE, target={"kind": "project", "id": "p1"}))

        self.assertEqual(specs, [])
