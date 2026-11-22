# -*- coding: utf-8 -*-
"""Архивные фоновые операции: бэкапы и экспорт разметки.

Проверяют контракт обвязки: сборка идёт в ``VC_STATE_ROOT/archives``,
совпавший архив переиспользуется, изменившееся состояние — пересобирает,
скачивание продлевает TTL, просроченное чистится. HTTP-слой скачивания
общий с остальными операциями и здесь не дублируется.

Прогон: cd backend && python manage.py test vc_jobs.tests_builtin_archive
"""

import json
import os
import time
import zipfile
from unittest import mock

import backup_format as fmt
import project_meta
import vc_jobs
from vc_fragments.tests import WorkspaceApiTestBase
from vc_jobs.archive_cache import ArchiveCache
from vc_jobs.builtin import register_builtin
from vc_jobs.builtin_archive import (ANNOTATIONS_TYPE, PROJECT_BACKUP_TYPE,
                                     TASK_BACKUP_TYPE, _build, _project_state,
                                     _stamp)
from vc_jobs.registry import JobRegistry
from vc_jobs.scheduler import JobSpec, Scheduler
from vc_jobs.store import JobStore


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def fetch_zip(resp):
    try:
        return b"".join(resp.streaming_content)
    finally:
        resp.close()


class ArchiveJobTestBase(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        self.state_root = os.path.join(self._tmpdir, ".state")
        self.env = mock.patch.dict(
            os.environ, {"VC_STATE_ROOT": self.state_root})
        self.env.start()
        self.addCleanup(self.env.stop)

        self.store = JobStore(os.path.join(self.state_root, "jobs"))
        self.registry = JobRegistry()
        register_builtin(self.registry)
        self.scheduler = Scheduler(
            self.store, self.registry,
            lane_capacities={"export": 1, "archive": 1, "import": 1,
                             "system": 1},
            max_workers=2, progress_interval=0.0)
        self.scheduler.start()
        self.addCleanup(vc_jobs.set_scheduler, None)
        self.addCleanup(self.scheduler.stop)
        vc_jobs.set_scheduler(self.scheduler)

    def submit(self, job_type, target=None, params=None):
        job, _ = self.scheduler.submit(JobSpec(
            type=job_type,
            target=target or {"kind": "task", "id": self.ws_id},
            params=params or {}))
        return job

    def wait_status(self, job_id, status, timeout=10.0):
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job_id).status == status, timeout),
            f"job {job_id} not {status}: "
            f"{self.scheduler.get(job_id).error}")
        return self.scheduler.get(job_id)

    def run_job(self, job_type, target=None, params=None):
        return self.wait_status(
            self.submit(job_type, target=target, params=params).id, "done")

    def archive_members(self, job):
        with zipfile.ZipFile(job.result["download_path"]) as zf:
            return zf.namelist()

    def create_project(self, name):
        resp = self.client.post(
            "/api/v1/projects/",
            data=json.dumps({"name": name}, ensure_ascii=False),
            content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()


class TaskBackupJobTests(ArchiveJobTestBase):
    def test_task_backup_returns_downloadable_zip(self):
        job = self.run_job(TASK_BACKUP_TYPE)

        self.assertEqual(job.result["download_name"],
                         f"{self.ws_id}-backup.zip")
        self.assertTrue(os.path.isfile(job.result["download_path"]))
        self.assertEqual(
            set(self.archive_members(job)),
            {"manifest.json", "task.json", "fragments.tsv",
             "visualization.mp4"})
        self.assertFalse(job.retention["keep"])
        self.assertIsNotNone(job.retention["expires_at"])

    def test_matching_archive_is_reused(self):
        first = self.run_job(TASK_BACKUP_TYPE)
        before = os.stat(first.result["download_path"]).st_mtime_ns

        second = self.run_job(TASK_BACKUP_TYPE)

        self.assertEqual(second.result["download_path"],
                         first.result["download_path"])
        self.assertEqual(
            os.stat(second.result["download_path"]).st_mtime_ns, before)

    def test_markup_change_rebuilds_archive(self):
        first = self.run_job(TASK_BACKUP_TYPE)
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "ab") as fh:
            fh.write(b"10\t11\tnew\n")

        second = self.run_job(TASK_BACKUP_TYPE)

        self.assertNotEqual(second.result["download_path"],
                            first.result["download_path"])
        with zipfile.ZipFile(second.result["download_path"]) as zf:
            self.assertIn(b"10\t11\tnew", zf.read("fragments.tsv"))

    def test_missing_task_fails_job(self):
        job = self.submit(
            TASK_BACKUP_TYPE, target={"kind": "task", "id": "no-such"})

        failed = self.wait_status(job.id, "error")

        self.assertIn("не найден", failed.error["message"])

    def test_old_archives_for_target_are_removed_after_rebuild(self):
        first = self.run_job(TASK_BACKUP_TYPE)
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "ab") as fh:
            fh.write(b"10\t11\tnew\n")

        second = self.run_job(TASK_BACKUP_TYPE)

        self.assertFalse(os.path.exists(first.result["download_path"]))
        self.assertTrue(os.path.exists(second.result["download_path"]))


class ProjectBackupJobTests(ArchiveJobTestBase):
    def setUp(self):
        super().setUp()
        self.project_id = "Архив"
        self.client.get(self.ws_list_url)
        self.create_project(self.project_id)
        resp = self.client.post(
            f"/api/v1/projects/{self.project_id}/tasks/",
            data=json.dumps({"task_id": self.ws_id}, ensure_ascii=False),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)

    def test_project_backup_contains_member_tasks(self):
        job = self.run_job(PROJECT_BACKUP_TYPE,
                           target={"kind": "project",
                                   "id": self.project_id})

        self.assertEqual(job.result["download_name"],
                         f"{self.project_id}-backup.zip")
        members = set(self.archive_members(job))
        self.assertIn(fmt.MANIFEST_NAME, members)
        self.assertIn("task_0/task.json", members)
        self.assertIn("task_0/fragments.tsv", members)

    def test_missing_project_fails_job(self):
        job = self.submit(
            PROJECT_BACKUP_TYPE,
            target={"kind": "project", "id": "no-such"})

        failed = self.wait_status(job.id, "error")

        self.assertIn("не найден", failed.error["message"])


class AnnotationsExportJobTests(ArchiveJobTestBase):
    def test_export_without_media_by_default(self):
        job = self.run_job(ANNOTATIONS_TYPE)

        self.assertEqual(job.result["download_name"],
                         f"{self.ws_id}-annotations.zip")
        members = set(self.archive_members(job))
        self.assertIn("manifest.json", members)
        self.assertIn("fragments.tsv", members)
        self.assertNotIn("visualization.mp4", members)

    def test_export_with_media_includes_videos(self):
        job = self.run_job(ANNOTATIONS_TYPE, params={"media": True})

        self.assertIn("visualization.mp4", set(self.archive_members(job)))

    def test_media_change_rebuilds_media_archive_only(self):
        without_media = self.run_job(ANNOTATIONS_TYPE)
        with_media = self.run_job(ANNOTATIONS_TYPE, params={"media": True})

        self.assertNotEqual(without_media.result["download_path"],
                            with_media.result["download_path"])


class _Ctx:
    def checkpoint(self):
        pass

    def progress(self, *args, **kwargs):
        pass


class ArchiveEdgeTests(ArchiveJobTestBase):
    def test_stamp_missing_path_is_none(self):
        self.assertIsNone(_stamp(os.path.join(self._tmpdir, "gone")))

    def test_failed_build_leaves_no_partial_archive(self):
        cache = ArchiveCache(os.path.join(self.state_root, "archives"))
        ctx = _Ctx()

        def writer(zf, context):
            raise RuntimeError("сборка сломалась")

        with self.assertRaises(RuntimeError):
            _build(cache, "backup.task", "k", "task:t", "a.zip", 1,
                   writer, ctx)

        directory = os.path.join(self.state_root, "archives", "backup.task")
        names = os.listdir(directory) if os.path.isdir(directory) else []
        self.assertEqual([n for n in names if n.startswith(".build-")], [])
        self.assertFalse(os.path.exists(os.path.join(directory, "k.zip")))

    def test_failed_build_survives_unremovable_temp(self):
        cache = ArchiveCache(os.path.join(self.state_root, "archives"))

        def writer(zf, context):
            raise RuntimeError("сборка сломалась")

        with mock.patch("vc_jobs.builtin_archive.os.remove",
                        side_effect=OSError("ro")):
            with self.assertRaises(RuntimeError):
                _build(cache, "backup.task", "k", "task:t", "a.zip", 1,
                       writer, _Ctx())

    def test_project_state_skips_missing_member(self):
        with mock.patch.object(project_meta, "task_project_ids",
                               return_value={"gone": "p"}), \
                mock.patch("vc_jobs.builtin_archive.get_workspace",
                           return_value=None):
            state = _project_state("p")

        self.assertEqual(state["tasks"], [])

    def test_annotations_export_missing_task_fails(self):
        job = self.submit(
            ANNOTATIONS_TYPE, target={"kind": "task", "id": "no-such"})

        failed = self.wait_status(job.id, "error")

        self.assertIn("не найдена", failed.error["message"])


class ArchiveApiTests(ArchiveJobTestBase):
    def test_download_extends_retention(self):
        job = self.run_job(TASK_BACKUP_TYPE)
        before = job.retention["expires_at"]
        time.sleep(0.01)

        resp = self.client.get(f"/api/v1/jobs/{job.id}/download")

        self.assertEqual(resp.status_code, 200)
        fetch_zip(resp)
        after = self.scheduler.get(job.id).retention["expires_at"]
        self.assertGreater(after, before)

    def test_expired_archive_is_rebuilt(self):
        first = self.run_job(TASK_BACKUP_TYPE)
        archive_path = first.result["download_path"]
        before = os.stat(archive_path).st_mtime_ns
        metadata = os.path.splitext(archive_path)[0] + ".json"
        with open(metadata) as fh:
            data = json.load(fh)
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        with open(metadata, "w") as fh:
            json.dump(data, fh)
        time.sleep(0.01)

        second = self.run_job(TASK_BACKUP_TYPE)

        self.assertEqual(second.result["download_path"], archive_path)
        self.assertGreater(os.stat(archive_path).st_mtime_ns, before)

    def test_cache_prune_removes_expired_entries(self):
        cache = ArchiveCache(os.path.join(self.state_root, "archives"))
        entry = cache.store(
            "backup.task", "k1", b"zip-bytes", "a.zip", ttl_days=0)
        with open(entry.metadata_path) as fh:
            data = json.load(fh)
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        with open(entry.metadata_path, "w") as fh:
            json.dump(data, fh)
        live = cache.store(
            "backup.task", "k2", b"zip-bytes", "b.zip", ttl_days=1)

        removed = cache.prune()

        self.assertIn(entry.archive_path, removed)
        self.assertFalse(os.path.exists(entry.archive_path))
        self.assertFalse(os.path.exists(entry.metadata_path))
        self.assertTrue(os.path.exists(live.archive_path))
