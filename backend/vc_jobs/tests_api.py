# -*- coding: utf-8 -*-
"""HTTP API фоновых операций: постановка, статус, отмена, скачивание.

Прогон: cd backend && python manage.py test vc_jobs.tests_api
"""

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

import vc_jobs
import workspace
from vc_jobs.activity import ActivityLog
from vc_jobs.registry import JobRegistry, JobType
from vc_jobs.scheduler import JobSpec, Scheduler
from vc_jobs.store import JobStore

JOBS = "/api/v1/jobs"
TYPES = "/api/v1/jobs/types"
SUMMARY = "/api/v1/jobs/summary"


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class JobsApiTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcjobsapi_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.workspaces = os.path.join(self.root, "workspaces")
        os.makedirs(self.workspaces)
        self.old_ws_root = workspace.WORKSPACE_ROOT
        workspace.WORKSPACE_ROOT = self.workspaces
        self.addCleanup(setattr, workspace, "WORKSPACE_ROOT",
                        self.old_ws_root)
        self.old_state = os.environ.get("VC_STATE_ROOT")
        os.environ["VC_STATE_ROOT"] = os.path.join(self.root, ".state")
        self.addCleanup(self._restore_state)
        self.old_proxy = os.environ.pop("VC_TRUST_PROXY", None)
        self.addCleanup(self._restore_proxy)

        self.store = JobStore(os.path.join(self.root, "jobs"))
        self.reg = JobRegistry()
        self.reg.register(JobType(
            key="test.echo", lane="export", label="Эхо",
            executor=lambda job, ctx: {"summary": "готово"}))
        self.reg.register(JobType(
            key="test.slow", lane="export", label="Долго",
            executor=self._slow))
        self.reg.register(JobType(
            key="test.archive", lane="archive", label="Архив",
            result_kind="file", retention_days=7,
            executor=self._archive))
        self.scheduler = Scheduler(
            self.store, self.reg,
            activity=ActivityLog(os.path.join(self.root, "activity")),
            lane_capacities={"export": 1, "archive": 1, "import": 1,
                             "system": 1},
            max_workers=2, progress_interval=0.0)
        self.scheduler.start()
        # stop() должен выполниться до удаления temp-каталога (cleanup LIFO).
        self.addCleanup(self.scheduler.stop)
        vc_jobs.set_scheduler(self.scheduler)
        self.addCleanup(vc_jobs.set_scheduler, None)
        self.release = threading.Event()

    def _restore_state(self):
        if self.old_state is None:
            os.environ.pop("VC_STATE_ROOT", None)
        else:
            os.environ["VC_STATE_ROOT"] = self.old_state

    def _restore_proxy(self):
        os.environ.pop("VC_TRUST_PROXY", None)
        if self.old_proxy is not None:
            os.environ["VC_TRUST_PROXY"] = self.old_proxy

    def _slow(self, job, ctx):
        ctx.progress(1, 2, "работаю")
        self.release.wait(5)
        return {"summary": "медленно готово"}

    def _archive(self, job, ctx):
        path = os.path.join(self.workspaces, "archive.zip")
        with open(path, "wb") as f:
            f.write(b"zipdata")
        return {"summary": "архив", "download_path": path,
                "download_name": "task.zip"}

    def post(self, url=JOBS, payload=None, **headers):
        return self.client.post(
            url, data=json.dumps(payload or {}),
            content_type="application/json", **headers)

    def create(self, type="test.echo", target=None, headers=None, **params):
        payload = {"type": type,
                   "target": target or {"kind": "task", "id": "t1"}}
        payload.update(params)
        response = self.post(payload=payload, **(headers or {}))
        self.assertEqual(response.status_code, 202)
        return response.json()["job"]

    def wait_done(self, job_id):
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job_id).status == "done"))
        return self.scheduler.get(job_id)

    def download(self, job_id):
        return self.client.get(f"{JOBS}/{job_id}/download")


class CreateTests(JobsApiTestBase):
    def test_create_returns_202_and_persists(self):
        response = self.post(payload={
            "type": "test.echo",
            "target": {"kind": "task", "id": "t1"},
            "params": {"format": "mp4"},
            "priority": 10,
        })

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertFalse(body["deduplicated"])
        job = body["job"]
        self.assertEqual(job["type"], "test.echo")
        self.assertEqual(job["owner"], "local")
        self.assertEqual(job["params"], {"format": "mp4"})
        self.assertEqual(job["priority"], 10)
        self.assertEqual(self.store.get(job["id"]).type, "test.echo")

    def test_dedupe_returns_same_job(self):
        first = self.create(type="test.slow",
                            target={"kind": "task", "id": "slow"})
        second = self.post(payload={
            "type": "test.slow",
            "target": {"kind": "task", "id": "slow"}})

        self.assertEqual(second.status_code, 202)
        self.assertTrue(second.json()["deduplicated"])
        self.assertEqual(second.json()["job"]["id"], first["id"])
        self.release.set()

    def test_unknown_type_is_400(self):
        response = self.post(payload={
            "type": "nope", "target": {"kind": "task", "id": "t"}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_bad_json_is_400(self):
        response = self.client.post(JOBS, data="{broken",
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_non_object_json_is_400(self):
        response = self.client.post(JOBS, data="[]",
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_missing_target_is_400(self):
        response = self.post(payload={"type": "test.echo", "target": {}})

        self.assertEqual(response.status_code, 400)

    def test_bad_params_is_400(self):
        response = self.post(payload={
            "type": "test.echo", "target": {"kind": "task", "id": "t"},
            "params": "x"})

        self.assertEqual(response.status_code, 400)

    def test_bad_priority_is_400(self):
        response = self.post(payload={
            "type": "test.echo", "target": {"kind": "task", "id": "t"},
            "priority": "high"})

        self.assertEqual(response.status_code, 400)

    def test_missing_type_is_400(self):
        response = self.post(payload={"target": {"kind": "task", "id": "t"}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("type", response.json()["error"])

    def test_non_object_target_is_400(self):
        response = self.post(payload={"type": "test.echo", "target": "task"})

        self.assertEqual(response.status_code, 400)

    def test_prerequisites_are_serialized_with_label_and_status(self):
        self.reg.register(JobType(
            key="test.parent", lane="system", label="Проверка видео",
            executor=self._slow))
        self.reg.register(JobType(
            key="test.child", lane="export", label="Экспорт",
            executor=lambda job, ctx: None,
            prerequisites=lambda spec: [JobSpec(
                type="test.parent", target=spec.target,
                owner=spec.owner)]))
        child = self.create(type="test.child")

        body = self.client.get(f"{JOBS}/{child['id']}").json()["job"]

        self.assertEqual(body["blocked_by"],
                         [body["prerequisites"][0]["id"]])
        self.assertEqual(body["prerequisites"][0]["type"], "test.parent")
        self.assertEqual(body["prerequisites"][0]["label"],
                         "Проверка видео")
        self.assertIn(body["prerequisites"][0]["status"],
                      ("queued", "running"))
        self.release.set()

    def test_bad_depends_on_is_400(self):
        response = self.post(payload={
            "type": "test.echo", "target": {"kind": "task", "id": "t"},
            "depends_on": [1]})

        self.assertEqual(response.status_code, 400)

    def test_non_string_idempotency_key_is_400(self):
        response = self.post(payload={
            "type": "test.echo", "target": {"kind": "task", "id": "t"},
            "idempotency_key": 42})

        self.assertEqual(response.status_code, 400)

    def test_body_owner_is_ignored(self):
        job = self.create(owner="root")

        self.assertEqual(job["owner"], "local")


class ReadTests(JobsApiTestBase):
    def test_detail_and_404(self):
        job = self.create()

        found = self.client.get(f"{JOBS}/{job['id']}")
        missing = self.client.get(f"{JOBS}/nope")

        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["job"]["id"], job["id"])
        self.assertEqual(missing.status_code, 404)

    def test_list_filters_and_cursor(self):
        for i in range(3):
            self.create(target={"kind": "task", "id": f"t{i}"})
        self.wait_done(self.scheduler.list()[0][0].id)

        response = self.client.get(JOBS, {"limit": 2})
        body = response.json()
        self.assertEqual(len(body["jobs"]), 2)
        self.assertTrue(body["next"])

        rest = self.client.get(JOBS, {"limit": 2,
                                      "cursor": body["next"]}).json()
        self.assertEqual(len(rest["jobs"]), 1)

        by_target = self.client.get(JOBS, {"target": "task:t1"}).json()
        self.assertEqual(len(by_target["jobs"]), 1)
        self.assertEqual(by_target["jobs"][0]["target"]["id"], "t1")

    def test_bad_limit_and_cursor_are_400(self):
        self.assertEqual(
            self.client.get(JOBS, {"limit": "x"}).status_code, 400)
        self.assertEqual(
            self.client.get(JOBS, {"cursor": "%%%"}).status_code, 400)

    def test_summary_and_types(self):
        self.create()
        self.wait_done(self.scheduler.list()[0][0].id)

        summary = self.client.get(SUMMARY).json()
        types = self.client.get(TYPES).json()

        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["active"], 0)
        self.assertEqual(
            [t["key"] for t in types["types"]],
            ["test.archive", "test.echo", "test.slow"])


class CancelRetryTests(JobsApiTestBase):
    def test_cancel_running_and_terminal_conflict(self):
        job = self.create(type="test.slow",
                          target={"kind": "task", "id": "s"})
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job["id"]).status == "running"))

        response = self.client.post(f"{JOBS}/{job['id']}/cancel")

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["job"]["cancel_requested"])
        self.release.set()
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job["id"]).status == "cancelled"))
        conflict = self.client.post(f"{JOBS}/{job['id']}/cancel")
        self.assertEqual(conflict.status_code, 409)

    def test_cancel_unknown_is_404(self):
        self.assertEqual(
            self.client.post(f"{JOBS}/nope/cancel").status_code, 404)

    def test_cancel_race_after_visibility_is_404(self):
        job = self.create()
        self.wait_done(job["id"])

        with mock.patch.object(self.scheduler, "cancel",
                               side_effect=KeyError(job["id"])):
            response = self.client.post(f"{JOBS}/{job['id']}/cancel")

        self.assertEqual(response.status_code, 404)

    def test_retry_failed_job(self):
        job = self.create()
        self.wait_done(job["id"])

        response = self.client.post(f"{JOBS}/{job['id']}/retry")

        self.assertEqual(response.status_code, 202)
        self.assertNotEqual(response.json()["job"]["id"], job["id"])
        self.assertEqual(response.json()["job"]["attempt"], 2)

    def test_retry_unknown_is_404(self):
        self.assertEqual(
            self.client.post(f"{JOBS}/nope/retry").status_code, 404)

    def test_retry_race_after_visibility_is_404(self):
        job = self.create()
        self.wait_done(job["id"])

        with mock.patch.object(self.scheduler, "retry",
                               side_effect=KeyError(job["id"])):
            response = self.client.post(f"{JOBS}/{job['id']}/retry")

        self.assertEqual(response.status_code, 404)

    def test_retry_active_is_409(self):
        job = self.create(type="test.slow",
                          target={"kind": "task", "id": "s"})
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job["id"]).status == "running"))

        response = self.client.post(f"{JOBS}/{job['id']}/retry")

        self.assertEqual(response.status_code, 409)
        self.release.set()


class DownloadTests(JobsApiTestBase):
    def test_download_done_archive(self):
        job = self.create(type="test.archive",
                          target={"kind": "task", "id": "t"})
        self.wait_done(job["id"])

        response = self.download(job["id"])

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("task.zip", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), b"zipdata")
        response.close()

    def test_download_not_finished_is_409(self):
        job = self.create(type="test.slow",
                          target={"kind": "task", "id": "s"})

        response = self.download(job["id"])

        self.assertEqual(response.status_code, 409)
        self.release.set()

    def test_download_without_file_is_409(self):
        job = self.create()
        self.wait_done(job["id"])

        self.assertEqual(self.download(job["id"]).status_code, 409)

    def test_download_missing_file_is_404(self):
        job = self.create(type="test.archive",
                          target={"kind": "task", "id": "t"})
        self.wait_done(job["id"])
        os.unlink(os.path.join(self.workspaces, "archive.zip"))

        self.assertEqual(self.download(job["id"]).status_code, 404)

    def test_download_outside_allowed_roots_is_403(self):
        job = self.create(type="test.archive",
                          target={"kind": "task", "id": "t"})
        self.wait_done(job["id"])
        self.scheduler.get(job["id"]).result["download_path"] = "/etc/passwd"

        self.assertEqual(self.download(job["id"]).status_code, 403)

    def test_download_expired_is_410(self):
        job = self.create(type="test.archive",
                          target={"kind": "task", "id": "t"})
        self.wait_done(job["id"])
        self.scheduler.get(job["id"]).retention = {
            "expires_at": "2000-01-01T00:00:00+00:00", "keep": False}

        self.assertEqual(self.download(job["id"]).status_code, 410)

    def test_download_stale_cache_entry_is_410(self):
        job = self.create(type="test.archive",
                          target={"kind": "task", "id": "t"})
        self.wait_done(job["id"])
        self.scheduler.get(job["id"]).result["cache"] = {
            "namespace": "backup.task", "key": "gone"}

        response = self.download(job["id"])

        self.assertEqual(response.status_code, 410)

    def test_download_unknown_is_404(self):
        self.assertEqual(self.download("nope").status_code, 404)


class OwnerTests(JobsApiTestBase):
    def test_headers_ignored_without_trusted_proxy(self):
        job = self.create(headers={"HTTP_X_VC_USER": "alice"})

        self.assertEqual(job["owner"], "local")

    def test_trusted_proxy_uses_user_and_hides_others(self):
        os.environ["VC_TRUST_PROXY"] = "1"

        alice = self.create(headers={"HTTP_X_VC_USER": "alice"})
        bob_list = self.client.get(
            JOBS, HTTP_X_VC_USER="bob").json()
        bob_detail = self.client.get(
            f"{JOBS}/{alice['id']}", HTTP_X_VC_USER="bob")

        self.assertEqual(alice["owner"], "alice")
        self.assertEqual(bob_list["jobs"], [])
        self.assertEqual(bob_detail.status_code, 404)

    def test_proxy_without_user_falls_back_to_local(self):
        os.environ["VC_TRUST_PROXY"] = "1"
        self.create(headers={"HTTP_X_VC_USER": "alice"})

        response = self.client.get(JOBS, HTTP_X_VC_ROLES="viewer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["jobs"]), 1)

    def test_admin_sees_all_and_can_filter_owner(self):
        os.environ["VC_TRUST_PROXY"] = "1"
        self.create(headers={"HTTP_X_VC_USER": "alice"})
        self.create(type="test.echo",
                    target={"kind": "task", "id": "other"},
                    headers={"HTTP_X_VC_USER": "bob"})

        admin = self.client.get(
            JOBS, HTTP_X_VC_USER="root",
            HTTP_X_VC_ROLES="admin").json()

        self.assertEqual(len(admin["jobs"]), 2)
        filtered = self.client.get(
            JOBS, {"owner": "alice"}, HTTP_X_VC_USER="root",
            HTTP_X_VC_ROLES="admin").json()
        self.assertEqual([j["owner"] for j in filtered["jobs"]], ["alice"])

    def test_cannot_cancel_foreign_job(self):
        os.environ["VC_TRUST_PROXY"] = "1"
        job = self.create(type="test.slow",
                          target={"kind": "task", "id": "s"},
                          headers={"HTTP_X_VC_USER": "alice"})
        self.assertTrue(wait_until(
            lambda: self.scheduler.get(job["id"]).status == "running"))

        response = self.client.post(
            f"{JOBS}/{job['id']}/cancel", HTTP_X_VC_USER="bob")

        self.assertEqual(response.status_code, 404)
        self.release.set()


class UploadOwnershipTests(JobsApiTestBase):
    def setUp(self):
        super().setUp()
        os.environ["VC_TRUST_PROXY"] = "1"

    def upload(self, user="alice"):
        archive = SimpleUploadedFile(
            "a.zip", b"zipdata", content_type="application/zip")
        response = self.client.post(
            f"{JOBS}/uploads", {"file": archive}, HTTP_X_VC_USER=user)
        self.assertEqual(response.status_code, 201)
        return response.json()

    def test_upload_records_owner(self):
        body = self.upload()

        self.assertEqual(body["owner"], "alice")

    def test_foreign_user_cannot_use_upload(self):
        body = self.upload()

        response = self.post(
            payload={"type": "test.echo",
                     "target": {"kind": "upload", "id": body["id"]}},
            HTTP_X_VC_USER="bob")

        self.assertEqual(response.status_code, 403)

    def test_foreign_user_cannot_use_upload_param(self):
        body = self.upload()

        response = self.post(
            payload={"type": "test.echo",
                     "target": {"kind": "task", "id": "t1"},
                     "params": {"upload_id": body["id"]}},
            HTTP_X_VC_USER="bob")

        self.assertEqual(response.status_code, 403)

    def test_owner_and_admin_may_use_upload(self):
        body = self.upload()
        payload = {"type": "test.echo",
                   "target": {"kind": "upload", "id": body["id"]}}

        owner = self.post(payload=payload, HTTP_X_VC_USER="alice")
        admin = self.post(payload=payload, HTTP_X_VC_USER="root",
                          HTTP_X_VC_ROLES="admin")

        self.assertEqual(owner.status_code, 202)
        self.assertEqual(admin.status_code, 202)

    def test_missing_upload_is_404(self):
        response = self.post(
            payload={"type": "test.echo",
                     "target": {"kind": "upload", "id": "a" * 32}},
            HTTP_X_VC_USER="alice")

        self.assertEqual(response.status_code, 404)

    def test_upload_disk_full_is_507(self):
        archive = SimpleUploadedFile("a.zip", b"zipdata")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch("vc_jobs.disk.free_bytes", return_value=0):
                response = self.client.post(
                    f"{JOBS}/uploads", {"file": archive},
                    HTTP_X_VC_USER="alice")
        finally:
            logging.disable(logging.NOTSET)

        self.assertEqual(response.status_code, 507)
        self.assertIn("места", response.json()["error"])
