# -*- coding: utf-8 -*-
"""Тесты потокового приёма (upload_handler + _place_stream).

Прогон:  cd backend && python manage.py test vc_pairs
"""

import os
import shutil
import tempfile
import time
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

import workspace as ws_module
from vc_fragments.tests import WorkspaceApiTestBase
from vc_pairs import upload_handler
from vc_pairs.upload_handler import (
    StagedUploadedFile,
    StagingUploadHandler,
    _progress_drop,
    _progress_set,
    get_upload_progress,
    sweep_staging,
)
from videocutter.standalone import workspace as ws_fs

HTTP_ACCEPTED = 202


class StagingHandlerTests(SimpleTestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_root = ws_module.WORKSPACE_ROOT
        ws_module.WORKSPACE_ROOT = self._tmpdir

    def tearDown(self):
        ws_module.WORKSPACE_ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _drive(self, handler, field="source", chunks=(b"abc", b"def")):
        from django.core.files.uploadhandler import StopFutureHandlers
        try:
            handler.new_file(field, "a.mp4", "video/mp4", sum(map(len, chunks)))
        except StopFutureHandlers:
            pass
        for i, chunk in enumerate(chunks):
            self.assertIsNone(handler.receive_data_chunk(chunk, i))
        return handler.file_complete(sum(map(len, chunks)))

    def test_stages_chunks_and_returns_staged_file(self):
        handler = StagingUploadHandler()
        uploaded = self._drive(handler)
        self.assertIsInstance(uploaded, StagedUploadedFile)
        self.assertEqual(uploaded.name, "a.mp4")
        with open(uploaded.staged_path, "rb") as f:
            self.assertEqual(f.read(), b"abcdef")
        # Temp/memory-хендлеры Django файл не видели: только стейджинг.
        self.assertTrue(uploaded.staged_path.startswith(
            os.path.join(self._tmpdir, ".incoming")))

    def test_interrupted_upload_cleans_partial(self):
        handler = StagingUploadHandler()
        from django.core.files.uploadhandler import StopFutureHandlers
        try:
            handler.new_file("source", "a.mp4", "video/mp4", 100)
        except StopFutureHandlers:
            pass
        handler.receive_data_chunk(b"abc", 0)
        staged = os.path.join(
            self._tmpdir, ".incoming", handler._batch, "source")
        self.assertTrue(os.path.isfile(staged))
        handler.upload_interrupted()
        self.assertFalse(os.path.exists(staged))
        self.assertFalse(os.path.exists(
            os.path.join(self._tmpdir, ".incoming", handler._batch)))

    def test_sweep_removes_only_stale(self):
        root = os.path.join(self._tmpdir, ".incoming")
        old, fresh = os.path.join(root, "old"), os.path.join(root, "fresh")
        os.makedirs(old)
        os.makedirs(fresh)
        ancient = time.time() - 7 * 3600
        os.utime(old, (ancient, ancient))
        self.assertEqual(sweep_staging(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(fresh))

    def test_same_fs_true_on_same_mount(self):
        a = os.path.join(self._tmpdir, "a.bin")
        with open(a, "wb") as f:
            f.write(b"x")
        self.assertTrue(ws_fs._same_fs(a, self._tmpdir))
        self.assertFalse(ws_fs._same_fs(a, os.path.join(self._tmpdir, "nope")))


class UploadStatusTests(SimpleTestCase):
    def test_unknown_and_malformed_id_404(self):
        resp = self.client.get("/api/v1/uploads/" + "0" * 32 + "/status")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/v1/uploads/not-a-uuid/status")
        self.assertEqual(resp.status_code, 404)

    def test_seeded_progress_returned(self):
        uid = "a" * 32
        _progress_set(uid, 12345, 100000)
        try:
            resp = self.client.get(f"/api/v1/uploads/{uid}/status")
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["received"], 12345)
            self.assertEqual(body["total"], 100000)
        finally:
            _progress_drop(uid)
        self.assertIsNone(get_upload_progress(uid))


class StagedPlacementTests(WorkspaceApiTestBase):
    """Интеграция: POST кладёт файл rename-ом, .incoming пуст после."""

    def _video(self, name: str = "staged.mp4") -> SimpleUploadedFile:
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def _incoming(self):
        root = os.path.join(self._tmpdir, ".incoming")
        out = []
        for dirpath, dirnames, filenames in os.walk(root):
            out.extend(dirnames)
            out.extend(filenames)
        return out

    def test_upload_uses_rename_no_leftovers(self):
        resp = self.client.post(
            self.ws_list_url, {"file": self._video(), "name": "staged-ws"})
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        ws_dir = os.path.join(self._tmpdir, "staged-ws")
        with open(os.path.join(ws_dir, "source.mp4"), "rb") as f:
            head = f.read(16)
        with open(os.path.join(
                os.path.dirname(__file__), "..", "testdata", "test.mp4"),
                "rb") as f:
            self.assertEqual(head, f.read(16))
        # Стейджинг потреблён rename-ом: мусора нет.
        self.assertEqual(self._incoming(), [])

    def test_fallback_copy_when_cross_fs(self):
        """Другая ФС: чанковая копия + чистка staged, содержимое цело."""
        resp_holder = {}

        real_replace = os.replace

        def fake_replace(src, dst):
            raise OSError(18, "Invalid cross-device link")

        with mock.patch("videocutter.standalone.workspace._same_fs",
                        return_value=False):
            with mock.patch.object(ws_fs.os, "replace",
                                   side_effect=fake_replace):
                resp = self.client.post(
                    self.ws_list_url,
                    {"file": self._video(), "name": "copy-ws"})
                resp_holder["resp"] = resp
        self.assertEqual(resp_holder["resp"].status_code, HTTP_ACCEPTED)
        with open(os.path.join(self._tmpdir, "copy-ws", "source.mp4"),
                  "rb") as f:
            self.assertTrue(len(f.read()) > 0)
        self.assertEqual(self._incoming(), [])
        self.assertIsNotNone(real_replace)
