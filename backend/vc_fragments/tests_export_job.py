# -*- coding: utf-8 -*-
"""Экспорт нарезок как тип фоновой операции.

Проверяет контракт: запись операции, прогресс, отмена с зачисткой,
ошибки и права на файлы. Сам ffmpeg не запускается — Exporter подменён.

Прогон: cd backend && python manage.py test vc_fragments.tests_export_job
"""

import os
import shutil
import stat
import threading
import time
from unittest import mock

import vc_jobs
import vc_fragments.views as export_views
from vc_jobs.builtin import register_builtin
from vc_jobs.registry import JobRegistry
from vc_jobs.scheduler import JobSpec, Scheduler
from vc_jobs.store import JobStore
from videocutter.core.exporter import ExportCancelled

from vc_fragments.tests import WorkspaceApiTestBase

EXPORT = "export.fragments"


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeExporter:
    """Пишет файлы детерминированно; опционально ждёт отмены на воротах."""

    gate = None
    seen_progress = []

    def __init__(self, source, out_dir, **kwargs):
        self.out_dir = out_dir
        self.prefix = os.path.splitext(os.path.basename(source))[0]

    def extract_fragments(self, fragments, progress=None, cancelled=None,
                          frame_ts_ranges=None):
        created = []
        for index, (start, end) in enumerate(fragments, 1):
            path = os.path.join(
                self.out_dir, f".{self.prefix}_fragment_{index}.mp4")
            with open(path, "wb") as f:
                f.write(b"fragment")
            created.append(path)
            if progress:
                progress(index, len(fragments), (start, end))
                type(self).seen_progress.append(index)
            if type(self).gate is not None:
                type(self).gate.wait(5)
            if cancelled is not None and cancelled():
                raise ExportCancelled("cancelled in test")
        return created

    def publish(self, partial_path):
        directory, name = os.path.split(partial_path)
        final_path = os.path.join(directory, name[1:])
        os.chmod(partial_path, 0o644)
        os.replace(partial_path, final_path)
        return final_path


class ExportJobTestBase(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        FakeExporter.gate = None
        FakeExporter.seen_progress = []
        self.check_verify = mock.patch.object(
            export_views, "_verify_export_cuts")
        self.check_verify.start()
        self.addCleanup(self.check_verify.stop)
        self.vpts = [i * 100000 for i in range(12)]
        self.pts_patch = mock.patch.object(
            export_views.frame_provider, "get_visible_pts",
            return_value=self.vpts)
        self.pts_patch.start()
        self.addCleanup(self.pts_patch.stop)
        self.unreliable_patch = mock.patch.object(
            export_views.frame_provider, "index_pts_unreliable",
            return_value=False)
        self.unreliable_patch.start()
        self.addCleanup(self.unreliable_patch.stop)

        self.store = JobStore(os.path.join(self._tmpdir, ".state", "jobs"))
        self.registry = JobRegistry()
        register_builtin(self.registry)
        self.scheduler = Scheduler(
            self.store, self.registry,
            lane_capacities={"export": 1, "archive": 1, "import": 1,
                             "system": 1},
            max_workers=2, progress_interval=0.0)
        self.scheduler.start()
        self.addCleanup(vc_jobs.set_scheduler, None)
        vc_jobs.set_scheduler(self.scheduler)

    def tearDown(self):
        self.scheduler.stop()
        super().tearDown()

    def export(self, target=None):
        job, _ = self.scheduler.submit(JobSpec(
            type=EXPORT, target=target or {"kind": "task", "id": self.ws_id}))
        return job

    def wait_status(self, job_id, status, timeout=5.0):
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job_id).status == status, timeout),
            f"job {job_id} not {status}: "
            f"{self.scheduler.get(job_id).error}")
        return self.scheduler.get(job_id)

    def run_export(self, exporter=FakeExporter):
        with mock.patch.object(export_views, "Exporter", exporter):
            job = self.export()
            return self.wait_status(job.id, "done")


class ExportJobRegistrationTests(ExportJobTestBase):
    def test_export_type_is_declared_for_ui_and_vdo(self):
        manifest = {item["key"]: item
                    for item in self.registry.manifest()}

        self.assertIn(EXPORT, manifest)
        item = manifest[EXPORT]
        self.assertEqual(item["lane"], "export")
        self.assertEqual(item["result_kind"], "files")
        self.assertTrue(item["cancellable"])
        self.assertTrue(item["resumable"])
        self.assertIsNone(item["retention_days"])


class ExportCoexistenceTests(ExportJobTestBase):
    def test_legacy_post_is_409_while_job_export_runs(self):
        FakeExporter.gate = threading.Event()
        self.addCleanup(FakeExporter.gate.set)
        with mock.patch.object(export_views, "Exporter", FakeExporter):
            job = self.export()
            self.assertTrue(wait_until(
                lambda: self.scheduler.get(job.id).progress["current"] == 1))

            response = self.client.post(self.export_url)
            FakeExporter.gate.set()
            self.wait_status(job.id, "done")

        self.assertEqual(response.status_code, 409)
        self.assertIn(job.id, response.json()["error"])

    def test_legacy_delete_is_409_while_job_export_runs(self):
        FakeExporter.gate = threading.Event()
        self.addCleanup(FakeExporter.gate.set)
        with mock.patch.object(export_views, "Exporter", FakeExporter):
            job = self.export()
            self.assertTrue(wait_until(
                lambda: self.scheduler.get(job.id).progress["current"] == 1))

            response = self.client.delete(self.export_url)
            FakeExporter.gate.set()
            self.wait_status(job.id, "done")

        self.assertEqual(response.status_code, 409)
        self.assertIn(job.id, response.json()["error"])

    def test_job_export_refuses_while_legacy_export_runs(self):
        FakeExporter.gate = threading.Event()
        self.addCleanup(FakeExporter.gate.set)
        with mock.patch.object(export_views, "Exporter", FakeExporter):
            started = self.client.post(self.export_url)
            self.assertEqual(started.status_code, 200)
            self.assertTrue(wait_until(
                lambda: (export_views.EXPORTS.get(self.ws_id) or {})
                .get("index") == 1))

            job = self.export()
            failed = self.wait_status(job.id, "error")
            FakeExporter.gate.set()

        self.assertIn("уже выполняется", failed.error["message"])


class ExportJobRunTests(ExportJobTestBase):
    def test_export_job_writes_files_and_reports_progress(self):
        job = self.run_export()

        out_dir = os.path.join(self.ws_dir, "exports")
        self.assertEqual(job.progress["current"], 2)
        self.assertEqual(job.progress["total"], 2)
        self.assertEqual(job.progress["unit"], "фрагмент")
        self.assertEqual(job.result["files"],
                         ["visualization_fragment_1.mp4",
                          "visualization_fragment_2.mp4"])
        self.assertEqual(
            job.result["urls"][0],
            f"/api/v1/pairs/{self.ws_id}/export/"
            "visualization_fragment_1.mp4")
        for name in job.result["files"]:
            path = os.path.join(out_dir, name)
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(out_dir).st_mode), 0o755)

    def test_second_submit_while_running_is_deduplicated(self):
        FakeExporter.gate = threading.Event()
        with mock.patch.object(export_views, "Exporter", FakeExporter):
            first = self.export()
            self.assertTrue(wait_until(
                lambda: self.scheduler.get(first.id).progress["current"] == 1))
            second, deduplicated = self.scheduler.submit(JobSpec(
                type=EXPORT,
                target={"kind": "task", "id": self.ws_id}))

            self.assertTrue(deduplicated)
            self.assertEqual(second.id, first.id)
            FakeExporter.gate.set()
            self.wait_status(first.id, "done")

    def test_cancel_running_cleans_partial_files(self):
        FakeExporter.gate = threading.Event()
        with mock.patch.object(export_views, "Exporter", FakeExporter):
            job = self.export()
            self.assertTrue(wait_until(
                lambda: self.scheduler.get(job.id).progress["current"] == 1))

            self.scheduler.cancel(job.id)
            FakeExporter.gate.set()
            self.wait_status(job.id, "cancelled")

        out_dir = os.path.join(self.ws_dir, "exports")
        self.assertEqual(os.listdir(out_dir), [])

    def test_missing_workspace_becomes_error(self):
        job = self.export(target={"kind": "task", "id": "ghost-task"})

        finished = self.wait_status(job.id, "error")
        self.assertIn("не найден", finished.error["message"])

    def test_task_without_fragments_becomes_error(self):
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "w") as f:
            f.write("start\tend\tcomment\n")

        job = self.export()

        finished = self.wait_status(job.id, "error")
        self.assertEqual(finished.error["message"],
                         "Нет фрагментов для экспорта")

    def test_export_job_is_persisted_after_completion(self):
        job = self.run_export()

        saved = self.store.get(job.id)

        self.assertEqual(saved.status, "done")
        self.assertEqual(saved.result["files"], job.result["files"])
