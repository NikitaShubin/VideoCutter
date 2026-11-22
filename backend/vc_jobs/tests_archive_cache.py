# -*- coding: utf-8 -*-
"""Кэш архивов: битые метаданные, TTL, чистка и атомарность.

Контракт кэша: источник истины — файлы на диске, а не память. Битая или
чужая запись не отдаётся, просроченная удаляется, неудачная сборка не
оставляет мусора. Эти тесты проверяют именно края, а не happy path —
он покрыт операциями бэкапов.

Прогон: cd backend && python manage.py test vc_jobs.tests_archive_cache
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs.archive_cache import ArchiveCache


class ArchiveCacheTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vccache_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cache = ArchiveCache(self.root)

    def paths(self, namespace="ns", key="k"):
        directory = os.path.join(self.root, namespace)
        return directory, os.path.join(directory, key + ".zip"), \
            os.path.join(directory, key + ".json")

    def write_entry(self, data, namespace="ns", key="k", archive=True):
        directory, archive_path, metadata = self.paths(namespace, key)
        os.makedirs(directory, exist_ok=True)
        if archive:
            with open(archive_path, "wb") as fh:
                fh.write(b"zip")
        with open(metadata, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return archive_path, metadata


class MetadataReadTests(ArchiveCacheTestBase):
    def test_corrupt_metadata_is_ignored(self):
        _, _, metadata = self.paths()
        os.makedirs(os.path.dirname(metadata))
        with open(metadata, "w", encoding="utf-8") as fh:
            fh.write("{broken")

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_foreign_metadata_shape_is_ignored(self):
        self.write_entry(["not", "a", "dict"])

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_metadata_for_another_key_is_ignored(self):
        self.write_entry({"key": "other", "archive": "k.zip"})

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_archive_name_outside_namespace_is_ignored(self):
        self.write_entry({"key": "k", "archive": "../k.zip"})

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_non_string_archive_name_is_ignored(self):
        self.write_entry({"key": "k", "archive": 123})

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_missing_archive_file_is_ignored(self):
        self.write_entry({"key": "k", "archive": "k.zip"}, archive=False)

        self.assertIsNone(self.cache.lookup("ns", "k"))

    def test_metadata_without_expiry_is_removed(self):
        archive, metadata = self.write_entry(
            {"key": "k", "archive": "k.zip"})

        self.assertIsNone(self.cache.lookup("ns", "k"))
        self.assertFalse(os.path.exists(archive))
        self.assertFalse(os.path.exists(metadata))

    def test_bad_expiry_is_removed(self):
        archive, _ = self.write_entry(
            {"key": "k", "archive": "k.zip", "expires_at": "nope"})

        self.assertIsNone(self.cache.lookup("ns", "k"))
        self.assertFalse(os.path.exists(archive))

    def test_naive_expiry_is_treated_as_utc(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            tzinfo=None).isoformat()
        self.write_entry({"key": "k", "archive": "k.zip",
                          "expires_at": future})

        self.assertIsNotNone(self.cache.lookup("ns", "k"))

    def test_expired_entry_is_removed(self):
        archive, metadata = self.write_entry({
            "key": "k", "archive": "k.zip",
            "expires_at": "2000-01-01T00:00:00+00:00"})

        self.assertIsNone(self.cache.lookup("ns", "k"))
        self.assertFalse(os.path.exists(archive))
        self.assertFalse(os.path.exists(metadata))


class CommitCleanupTests(ArchiveCacheTestBase):
    def test_failed_metadata_write_removes_published_archive(self):
        temp = self.cache.new_temp("ns")
        with open(temp, "wb") as fh:
            fh.write(b"zip")

        with mock.patch.object(self.cache, "_write",
                               side_effect=OSError("диск")):
            with self.assertRaises(OSError):
                self.cache.commit(
                    "ns", "k", temp, target="task:t", download_name="a.zip",
                    ttl_days=1)

        _, archive, _ = self.paths()
        self.assertFalse(os.path.exists(archive))

    def test_failed_store_removes_temporary_file(self):
        with mock.patch.object(self.cache, "commit",
                               side_effect=RuntimeError("сбой")):
            with self.assertRaises(RuntimeError):
                self.cache.store("ns", "k", b"zip", "a.zip")

        directory = os.path.join(self.root, "ns")
        self.assertEqual(
            [name for name in os.listdir(directory)
             if name.startswith(".build-")], [])

    def test_failed_metadata_write_with_unremovable_archive_still_raises(self):
        temp = self.cache.new_temp("ns")
        with open(temp, "wb") as fh:
            fh.write(b"zip")

        with mock.patch.object(self.cache, "_write",
                               side_effect=OSError("диск")), \
                mock.patch("vc_jobs.archive_cache.os.remove",
                           side_effect=OSError("ro")):
            with self.assertRaises(OSError):
                self.cache.commit(
                    "ns", "k", temp, target="task:t", download_name="a.zip",
                    ttl_days=1)

    def test_failed_store_with_unremovable_temp_still_raises(self):
        with mock.patch.object(self.cache, "commit",
                               side_effect=RuntimeError("сбой")), \
                mock.patch("vc_jobs.archive_cache.os.remove",
                           side_effect=OSError("ro")):
            with self.assertRaises(RuntimeError):
                self.cache.store("ns", "k", b"zip", "a.zip")


class TouchTests(ArchiveCacheTestBase):
    def test_touch_missing_entry_returns_none(self):
        self.assertIsNone(self.cache.touch("ns", "missing", 1))

    def test_touch_survives_utime_error(self):
        entry = self.cache.store("ns", "k", b"zip", "a.zip", ttl_days=1)

        with mock.patch("vc_jobs.archive_cache.os.utime",
                        side_effect=OSError("ro")):
            touched = self.cache.touch("ns", "k", 2)

        self.assertIsNotNone(touched)
        self.assertGreater(touched.expires_at, entry.expires_at)


class RemoveTests(ArchiveCacheTestBase):
    def test_remove_target_keeps_current_version_and_other_targets(self):
        old = self.cache.store("ns", "old", b"1", "old.zip",
                               target="task:t1")
        keep = self.cache.store("ns", "keep", b"2", "keep.zip",
                                target="task:t1")
        other = self.cache.store("ns", "other", b"3", "other.zip",
                                 target="task:t2")

        removed = self.cache.remove_target("ns", "task:t1", keep_key="keep")

        self.assertEqual(removed, [old.archive_path])
        self.assertFalse(os.path.exists(old.archive_path))
        self.assertTrue(os.path.exists(keep.archive_path))
        self.assertTrue(os.path.exists(other.archive_path))

    def test_remove_survives_already_missing_and_unremovable_files(self):
        self.cache.store("ns", "k", b"zip", "a.zip", target="task:t")

        with mock.patch("vc_jobs.archive_cache.os.remove",
                        side_effect=[FileNotFoundError(), OSError("ro")]):
            removed = self.cache.remove_target("ns", "task:t")

        self.assertEqual(len(removed), 1)

    def test_remove_target_missing_namespace_is_noop(self):
        self.assertEqual(self.cache.remove_target("missing", "task:t"), [])

    def test_prune_without_root_is_empty(self):
        cache = ArchiveCache(os.path.join(self.root, "absent"))

        self.assertEqual(cache.prune(), [])

    def test_clear_removes_root(self):
        self.cache.store("ns", "k", b"zip", "a.zip")

        self.cache.clear()

        self.assertFalse(os.path.exists(self.root))


class PruneTests(ArchiveCacheTestBase):
    def test_prune_removes_orphaned_build_files_after_a_day(self):
        directory = os.path.join(self.root, "ns")
        os.makedirs(directory)
        old = os.path.join(directory, ".build-old.zip")
        fresh = os.path.join(directory, ".build-fresh.zip")
        for path in (old, fresh):
            with open(path, "wb") as fh:
                fh.write(b"x")
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=2)).timestamp()
        os.utime(old, (old_time, old_time))

        removed = self.cache.prune(now=now)

        self.assertIn(old, removed)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_prune_survives_unremovable_build_file(self):
        directory = os.path.join(self.root, "ns")
        os.makedirs(directory)
        old = os.path.join(directory, ".build-old.zip")
        with open(old, "wb") as fh:
            fh.write(b"x")
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=2)).timestamp()
        os.utime(old, (old_time, old_time))

        with mock.patch("vc_jobs.archive_cache.os.remove",
                        side_effect=OSError("ro")):
            removed = self.cache.prune(now=now)

        self.assertEqual(removed, [])
        self.assertTrue(os.path.exists(old))

    def test_prune_survives_unlistable_namespace(self):
        os.makedirs(os.path.join(self.root, "ns"))
        real_listdir = os.listdir
        calls = []

        def listdir(path):
            calls.append(path)
            if len(calls) == 1:
                return real_listdir(path)
            raise OSError("нет доступа")

        with mock.patch("vc_jobs.archive_cache.os.listdir",
                        side_effect=listdir):
            removed = self.cache.prune()

        self.assertEqual(removed, [])
