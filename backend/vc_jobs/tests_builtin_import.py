# -*- coding: utf-8 -*-
"""Импорт архивов как фоновые операции.

Загрузка отделена от запуска: файл один раз попадает в
``VC_STATE_ROOT/uploads``, операция получает только идентификатор. Это
позволяет повторять запрос без повторной передачи гигабайтов и не держать
HTTP-соединение на время распаковки.

Прогон: cd backend && python manage.py test vc_jobs.tests_builtin_import
"""

import os
import time
from types import SimpleNamespace
from unittest import mock

import backup_format as fmt
import task_meta
from django.core.files.uploadedfile import SimpleUploadedFile
from vc_jobs.builtin_archive import TASK_BACKUP_TYPE
from vc_jobs.builtin_import import (ANNOTATIONS_IMPORT_TYPE, BACKUP_IMPORT_TYPE,
                                    _backup_import)
from vc_jobs.tests_builtin_archive import (ArchiveJobTestBase, _Ctx, fetch_zip,
                                           wait_until)
from workspace import parse_fragments_tsv_strict


class ImportJobTestBase(ArchiveJobTestBase):
    def upload(self, data: bytes, name: str = "archive.zip"):
        resp = self.client.post(
            "/api/v1/jobs/uploads",
            {"file": SimpleUploadedFile(
                name, data, content_type="application/zip")})
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def backup_bytes(self, ws_id=None):
        resp = self.client.get(
            f"/api/v1/workspaces/{ws_id or self.ws_id}/backup")
        self.assertEqual(resp.status_code, 200)
        return fetch_zip(resp)

    def annotations_bytes(self, ws_id=None):
        resp = self.client.get(
            f"/api/v1/pairs/{ws_id or self.ws_id}/annotations")
        self.assertEqual(resp.status_code, 200)
        return fetch_zip(resp)

    def wait_done(self, job_id, timeout=15.0):
        return self.wait_status(job_id, "done", timeout)


class UploadTests(ImportJobTestBase):
    def test_upload_returns_id_and_persists_file(self):
        body = self.upload(b"zip-bytes", "backup.zip")

        self.assertEqual(body["name"], "backup.zip")
        self.assertEqual(body["size"], len(b"zip-bytes"))
        path = os.path.join(self.state_root, "uploads",
                            body["id"] + ".zip")
        self.assertTrue(os.path.isfile(path))

    def test_unknown_upload_is_rejected(self):
        job = self.submit(
            BACKUP_IMPORT_TYPE, target={"kind": "upload", "id": "missing"})

        failed = self.wait_status(job.id, "error")

        self.assertIn("загруженный файл не найден", failed.error["message"])

    def test_upload_requires_file(self):
        response = self.client.post("/api/v1/jobs/uploads", {})

        self.assertEqual(response.status_code, 400)

    def test_oversized_upload_is_rejected(self):
        with mock.patch.dict(os.environ, {"VC_JOB_UPLOAD_MAX_MB": "1"}):
            response = self.client.post(
                "/api/v1/jobs/uploads",
                {"file": SimpleUploadedFile(
                    "big.zip", b"x" * (2 * 1024 * 1024),
                    content_type="application/zip")})

        self.assertEqual(response.status_code, 400)


class BackupImportTests(ImportJobTestBase):
    def test_backup_import_restores_task_with_rename(self):
        upload = self.upload(self.backup_bytes())

        job = self.submit(
            BACKUP_IMPORT_TYPE, target={"kind": "upload", "id": upload["id"]})
        done = self.wait_done(job.id)

        self.assertEqual(done.result["task_id"], f"{self.ws_id}_1")
        self.assertTrue(os.path.isdir(
            os.path.join(self._tmpdir, f"{self.ws_id}_1")))

    def test_on_conflict_error_fails_job(self):
        upload = self.upload(self.backup_bytes())

        job = self.submit(
            BACKUP_IMPORT_TYPE, target={"kind": "upload", "id": upload["id"]},
            params={"on_conflict": "error"})
        failed = self.wait_status(job.id, "error")

        self.assertIn("уже существует", failed.error["message"])

    def test_invalid_on_conflict_fails_job(self):
        upload = self.upload(self.backup_bytes())

        job = self.submit(
            BACKUP_IMPORT_TYPE, target={"kind": "upload", "id": upload["id"]},
            params={"on_conflict": "sometimes"})
        failed = self.wait_status(job.id, "error")

        self.assertIn("on_conflict", failed.error["message"])

    def test_broken_archive_fails_job(self):
        upload = self.upload(b"not a zip")

        job = self.submit(
            BACKUP_IMPORT_TYPE, target={"kind": "upload", "id": upload["id"]})
        failed = self.wait_status(job.id, "error")

        self.assertIn("архив", failed.error["message"].lower())

    def test_project_import_reports_project_and_tasks(self):
        upload = self.upload(b"project-archive")
        job = SimpleNamespace(
            target={"kind": "upload", "id": upload["id"]}, params={})
        imported = {"kind": fmt.KIND_BACKUP, "id": "p1",
                    "tasks": ["t1", "t2"]}

        with mock.patch("vc_jobs.builtin_import.import_archive",
                        return_value=imported):
            result = _backup_import(job, _Ctx())

        self.assertEqual(result.extra["project_id"], "p1")
        self.assertEqual(result.extra["tasks"], ["t1", "t2"])
        self.assertIn("Импорт проекта", result.summary)


class AnnotationsImportTests(ImportJobTestBase):
    def test_annotations_import_replaces_markup(self):
        upload = self.upload(self.annotations_bytes())
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "w") as fh:
            fh.write("start\\tend\\tcomment\\n")

        job = self.submit(
            ANNOTATIONS_IMPORT_TYPE, params={"upload_id": upload["id"]})
        done = self.wait_done(job.id)

        self.assertEqual(done.result["fragments"], 2)
        with open(os.path.join(self.ws_dir, "fragments.tsv"), "rb") as fh:
            fragments, _position, problem = parse_fragments_tsv_strict(
                fh.read(), 10)
        self.assertIsNone(problem)
        self.assertEqual(len(fragments), 2)

    def test_annotations_import_requires_upload_id(self):
        job = self.submit(ANNOTATIONS_IMPORT_TYPE)

        failed = self.wait_status(job.id, "error")

        self.assertIn("upload_id", failed.error["message"])

    def test_wrong_archive_kind_fails_job(self):
        upload = self.upload(self.backup_bytes())

        job = self.submit(
            ANNOTATIONS_IMPORT_TYPE, params={"upload_id": upload["id"]})
        failed = self.wait_status(job.id, "error")

        self.assertIn("архив разметки", failed.error["message"])

    def test_annotations_import_missing_task_fails(self):
        job = self.submit(
            ANNOTATIONS_IMPORT_TYPE,
            target={"kind": "task", "id": "no-such"},
            params={"upload_id": "x" * 32})

        failed = self.wait_status(job.id, "error")

        self.assertIn("не найдена", failed.error["message"])
