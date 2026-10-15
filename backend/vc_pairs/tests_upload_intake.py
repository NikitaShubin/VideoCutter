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


class UploadHandlerInitGapsTests(SimpleTestCase):
    """Покрытие __init__: битый request, валидный upload_id, плохой length."""

    def test_init_request_get_raises(self):
        bad = mock.Mock()
        bad.GET.get.side_effect = Exception("boom")
        handler = StagingUploadHandler(bad)
        self.assertIsNone(handler._upload_id)

    def test_init_valid_upload_id_sets_total(self):
        from types import SimpleNamespace

        uid = "b" * 32
        req = SimpleNamespace(
            GET={"upload_id": uid}, META={"CONTENT_LENGTH": "123"})
        handler = StagingUploadHandler(req)
        self.assertEqual(handler._upload_id, uid)
        self.assertEqual(handler._total, 123)

    def test_init_valid_upload_id_bad_length_falls_back(self):
        from types import SimpleNamespace

        uid = "c" * 32
        req = SimpleNamespace(
            GET={"upload_id": uid}, META={"CONTENT_LENGTH": "not-a-number"})
        handler = StagingUploadHandler(req)
        self.assertEqual(handler._upload_id, uid)
        self.assertEqual(handler._total, 0)


class UploadHandlerSweepGapsTests(SimpleTestCase):
    """Покрытие sweep_staging / _progress_sweep: файлы, OSError, stale."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_root = ws_module.WORKSPACE_ROOT
        ws_module.WORKSPACE_ROOT = self._tmpdir

    def tearDown(self):
        ws_module.WORKSPACE_ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_progress_sweep_drops_only_stale(self):
        stale_uid = "d" * 32
        fresh_uid = "e" * 32
        _progress_set(stale_uid, 10, 100)
        _progress_set(fresh_uid, 20, 100)
        try:
            upload_handler._UPLOAD_PROGRESS[stale_uid][2] = (
                time.time() - 7 * 3600)
            upload_handler._progress_sweep()
            self.assertIsNone(get_upload_progress(stale_uid))
            self.assertIsNotNone(get_upload_progress(fresh_uid))
        finally:
            _progress_drop(stale_uid)
            _progress_drop(fresh_uid)

    def test_sweep_listdir_error_returns_zero(self):
        with mock.patch.object(
                upload_handler.os, "listdir",
                side_effect=OSError("boom")):
            self.assertEqual(sweep_staging(), 0)

    def test_sweep_removes_stale_file(self):
        root = os.path.join(self._tmpdir, ".incoming")
        os.makedirs(root, exist_ok=True)
        stale = os.path.join(root, "lonely.bin")
        with open(stale, "wb") as f:
            f.write(b"x")
        ancient = time.time() - 7 * 3600
        os.utime(stale, (ancient, ancient))
        self.assertEqual(sweep_staging(), 1)
        self.assertFalse(os.path.exists(stale))

    def test_sweep_remove_error_continues(self):
        root = os.path.join(self._tmpdir, ".incoming")
        os.makedirs(root, exist_ok=True)
        stale = os.path.join(root, "locked.bin")
        with open(stale, "wb") as f:
            f.write(b"x")
        ancient = time.time() - 7 * 3600
        os.utime(stale, (ancient, ancient))
        with mock.patch.object(
                upload_handler.os, "remove",
                side_effect=OSError("boom")):
            self.assertEqual(sweep_staging(), 0)
        self.assertTrue(os.path.isfile(stale))

    def test_new_file_sweep_failure_ignored(self):
        from django.core.files.uploadhandler import StopFutureHandlers

        with mock.patch.object(
                upload_handler, "sweep_staging",
                side_effect=RuntimeError("boom")):
            handler = StagingUploadHandler()
            try:
                handler.new_file("source", "a.mp4", "video/mp4", 3)
            except StopFutureHandlers:
                pass
            else:
                self.fail("StopFutureHandlers not raised")
            staged = os.path.join(
                self._tmpdir, ".incoming", handler._batch, "source")
            self.assertTrue(os.path.isfile(staged))
            handler.upload_interrupted()


class UploadHandlerChunkGapsTests(SimpleTestCase):
    """Покрытие receive/file_complete/interrupted: None-ветки, OSError, прогресс."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_root = ws_module.WORKSPACE_ROOT
        ws_module.WORKSPACE_ROOT = self._tmpdir

    def tearDown(self):
        ws_module.WORKSPACE_ROOT = self._orig_root
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        for uid in ("f" * 32, "9" * 32):
            _progress_drop(uid)

    def _started(self, handler, field="source", chunks=(b"abc",)):
        from django.core.files.uploadhandler import StopFutureHandlers

        try:
            handler.new_file(field, "a.mp4", "video/mp4",
                             sum(map(len, chunks)))
        except StopFutureHandlers:
            pass
        for i, chunk in enumerate(chunks):
            handler.receive_data_chunk(chunk, i)
        return handler

    def test_receive_without_file_returns_none(self):
        handler = StagingUploadHandler()
        self.assertIsNone(handler.receive_data_chunk(b"abc", 0))

    def test_receive_updates_progress(self):
        from types import SimpleNamespace

        uid = "f" * 32
        req = SimpleNamespace(
            GET={"upload_id": uid}, META={"CONTENT_LENGTH": "100"})
        handler = StagingUploadHandler(req)
        from django.core.files.uploadhandler import StopFutureHandlers

        try:
            handler.new_file("source", "a.mp4", "video/mp4", 3)
        except StopFutureHandlers:
            pass
        handler.receive_data_chunk(b"abc", 0)
        try:
            self.assertEqual(get_upload_progress(uid), (3, 100))
        finally:
            handler.upload_interrupted()
        self.assertIsNone(get_upload_progress(uid))

    def test_file_complete_without_file_returns_none(self):
        handler = StagingUploadHandler()
        self.assertIsNone(handler.file_complete(0))

    def test_file_complete_close_error_ignored(self):
        handler = self._started(StagingUploadHandler(), chunks=(b"data",))
        staged, real_handle, info = handler._current
        try:
            real_handle.close()
        except Exception:
            pass
        fake = mock.Mock()
        fake.close.side_effect = OSError("boom")
        handler._current = (staged, fake, info)
        uploaded = handler.file_complete(4)
        try:
            self.assertIsInstance(uploaded, StagedUploadedFile)
            with open(uploaded.staged_path, "rb") as f:
                self.assertEqual(f.read(), b"data")
        finally:
            try:
                uploaded.file.close()
            except Exception:
                pass

    def test_interrupted_drops_progress_without_file(self):
        uid = "9" * 32
        _progress_set(uid, 5, 10)
        handler = StagingUploadHandler()
        handler._upload_id = uid
        handler._current = None
        handler.upload_interrupted()
        self.assertIsNone(get_upload_progress(uid))

    def test_interrupted_close_error_ignored(self):
        handler = self._started(StagingUploadHandler(), chunks=(b"xyz",))
        staged, real_handle, info = handler._current
        try:
            real_handle.close()
        except Exception:
            pass
        fake = mock.Mock()
        fake.close.side_effect = OSError("boom")
        handler._current = (staged, fake, info)
        batch_dir = os.path.dirname(staged)
        handler.upload_interrupted()
        self.assertFalse(os.path.exists(staged))
        self.assertFalse(os.path.exists(batch_dir))

    def test_interrupted_remove_error_ignored(self):
        handler = self._started(StagingUploadHandler(), chunks=(b"xyz",))
        with mock.patch.object(
                upload_handler.os, "remove",
                side_effect=OSError("boom")):
            handler.upload_interrupted()
        self.assertIsNone(handler._current)

    def test_interrupted_rmdir_error_ignored(self):
        handler = self._started(StagingUploadHandler(), chunks=(b"xyz",))
        staged = os.path.join(
            self._tmpdir, ".incoming", handler._batch, "source")
        keep = os.path.join(os.path.dirname(staged), "keep.bin")
        with open(keep, "wb") as f:
            f.write(b"k")
        handler.upload_interrupted()
        self.assertFalse(os.path.exists(staged))
        self.assertTrue(os.path.isdir(os.path.dirname(keep)))
        self.assertTrue(os.path.isfile(keep))
