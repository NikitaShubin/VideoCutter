# -*- coding: utf-8 -*-
"""Загруженные архивы для фонового импорта: TTL, чистка, защита id.

Файл живёт в ``VC_STATE_ROOT/uploads`` и не является доверенным вводом:
идентификатор проверяется как UUID, старые и недописанные файлы убираются,
а сбой записи не оставляет ``.part-`` мусора.

Прогон: cd backend && python manage.py test vc_jobs.tests_uploads
"""

import os
import shutil
import tempfile
import time
import uuid
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs import disk, uploads


class FakeUpload:
    def __init__(self, size=3, name="archive.zip", chunks=(b"zip",),
                 error=None):
        self.size = size
        self.name = name
        self._chunks = chunks
        self._error = error

    def chunks(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class UploadsTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcuploads_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state")
        self.env = mock.patch.dict(
            os.environ, {"VC_STATE_ROOT": self.state})
        self.env.start()
        self.addCleanup(self.env.stop)

    def directory(self):
        return os.path.join(self.state, "uploads")

    def make_file(self, name, age_s=0):
        os.makedirs(self.directory(), exist_ok=True)
        path = os.path.join(self.directory(), name)
        with open(path, "wb") as fh:
            fh.write(b"x")
        if age_s:
            stamp = time.time() - age_s
            os.utime(path, (stamp, stamp))
        return path


class SaveTests(UploadsTestBase):
    def test_save_persists_file_and_returns_metadata(self):
        body = uploads.save(FakeUpload(chunks=(b"ab", b"cd")))

        self.assertEqual(body["size"], 3)
        self.assertEqual(body["name"], "archive.zip")
        path = os.path.join(self.directory(), body["id"] + ".zip")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"abcd")

    def test_bad_size_env_falls_back_to_default_limit(self):
        with mock.patch.dict(os.environ,
                             {"VC_JOB_UPLOAD_MAX_MB": "not-a-number"}):
            body = uploads.save(FakeUpload(size=3))

        self.assertTrue(os.path.isfile(
            os.path.join(self.directory(), body["id"] + ".zip")))

    def test_oversized_upload_is_rejected(self):
        with mock.patch.dict(os.environ, {"VC_JOB_UPLOAD_MAX_MB": "1"}):
            with self.assertRaises(ValueError):
                uploads.save(FakeUpload(size=2 * 1024 * 1024))

    def test_save_checks_free_space_before_creating_file(self):
        with mock.patch("vc_jobs.disk.free_bytes", return_value=0):
            with self.assertRaises(disk.InsufficientSpace):
                uploads.save(FakeUpload())

        self.assertEqual(os.listdir(self.directory()), [])

    def test_failed_write_removes_partial_file(self):
        upload = FakeUpload(error=OSError("диск"))

        with self.assertRaises(OSError):
            uploads.save(upload)

        leftovers = [name for name in os.listdir(self.directory())
                     if name.startswith(".part-")]
        self.assertEqual(leftovers, [])

    def test_failed_write_with_unremovable_partial_still_raises(self):
        upload = FakeUpload(error=OSError("диск"))

        with mock.patch("vc_jobs.uploads.os.remove",
                        side_effect=OSError("ro")):
            with self.assertRaises(OSError):
                uploads.save(upload)


class PruneTests(UploadsTestBase):
    def test_prune_missing_directory_is_noop(self):
        uploads.prune()

    def test_bad_retention_env_falls_back_to_one_day(self):
        old = self.make_file("old.zip", age_s=2 * 86400)

        with mock.patch.dict(os.environ,
                             {"VC_JOB_UPLOAD_RETENTION_DAYS": "nope"}):
            uploads.prune()

        self.assertFalse(os.path.exists(old))

    def test_prune_removes_old_files_and_keeps_fresh(self):
        old = self.make_file("old.zip", age_s=2 * 86400)
        fresh = self.make_file("fresh.zip")

        uploads.prune()

        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_prune_survives_unremovable_file(self):
        old = self.make_file("old.zip", age_s=2 * 86400)

        with mock.patch("vc_jobs.uploads.os.remove",
                        side_effect=OSError("ro")):
            uploads.prune()

        self.assertTrue(os.path.exists(old))


class OwnershipTests(UploadsTestBase):
    def test_save_remembers_owner(self):
        body = uploads.save(FakeUpload(), owner="alice")

        self.assertEqual(body["owner"], "alice")
        self.assertEqual(uploads.owner(body["id"]), "alice")

    def test_legacy_upload_without_sidecar_is_local(self):
        path = self.make_file("a" * 32 + ".zip")

        self.assertEqual(uploads.owner("a" * 32), "local")
        self.assertTrue(os.path.isfile(path))

    def test_owner_of_missing_upload_is_none(self):
        self.assertIsNone(uploads.owner(uuid.uuid4().hex))
        self.assertIsNone(uploads.owner("../bad"))

    def test_broken_sidecar_falls_back_to_local(self):
        body = uploads.save(FakeUpload())
        with open(os.path.join(
                self.directory(), body["id"] + ".json"), "w") as fh:
            fh.write("{broken")

        self.assertEqual(uploads.owner(body["id"]), "local")


class PinTests(UploadsTestBase):
    def test_prune_keeps_upload_pinned_by_active_job(self):
        body = uploads.save(FakeUpload())
        path = os.path.join(self.directory(), body["id"] + ".zip")
        old = time.time() - 2 * 86400
        os.utime(path, (old, old))

        uploads.prune(pinned={body["id"]})

        self.assertTrue(os.path.isfile(path))

    def test_prune_removes_sidecar_with_archive(self):
        body = uploads.save(FakeUpload())
        path = os.path.join(self.directory(), body["id"] + ".zip")
        meta = os.path.join(self.directory(), body["id"] + ".json")
        old = time.time() - 2 * 86400
        os.utime(path, (old, old))
        os.utime(meta, (old, old))

        uploads.prune()

        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(meta))


class ResolveTests(UploadsTestBase):
    def test_resolve_returns_saved_file(self):
        body = uploads.save(FakeUpload())

        path = uploads.resolve(body["id"])

        self.assertTrue(os.path.isfile(path))

    def test_resolve_rejects_non_uuid_ids(self):
        self.assertIsNone(uploads.resolve("x" * 32))
        self.assertIsNone(uploads.resolve("../" + "a" * 29))
        self.assertIsNone(uploads.resolve(None))
        self.assertIsNone(uploads.resolve("short"))

    def test_resolve_unknown_uuid_is_none(self):
        self.assertIsNone(uploads.resolve(uuid.uuid4().hex))

    def test_clear_removes_uploads_directory(self):
        uploads.save(FakeUpload())

        uploads.clear()

        self.assertFalse(os.path.exists(self.directory()))
