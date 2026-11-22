# -*- coding: utf-8 -*-
"""Проверка свободного места: запас, отсутствующий путь, сбой statvfs."""

import os
import shutil
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs import disk


class MinFreeTests(SimpleTestCase):
    def test_env_value_is_used(self):
        with mock.patch.dict(os.environ, {"VC_DISK_MIN_FREE_MB": "7"}):
            self.assertEqual(disk.min_free_bytes(), 7 * 1024 * 1024)

    def test_bad_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"VC_DISK_MIN_FREE_MB": "nope"}):
            self.assertEqual(disk.min_free_bytes(), 512 * 1024 * 1024)

    def test_negative_env_is_zero(self):
        with mock.patch.dict(os.environ, {"VC_DISK_MIN_FREE_MB": "-1"}):
            self.assertEqual(disk.min_free_bytes(), 0)


class FreeBytesTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcdisk_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_existing_path_uses_its_filesystem(self):
        self.assertGreater(disk.free_bytes(self.root), 0)

    def test_missing_nested_path_uses_nearest_parent(self):
        missing = os.path.join(self.root, "a", "b", "c")
        self.assertGreater(disk.free_bytes(missing), 0)

    def test_disk_usage_failure_is_zero(self):
        with mock.patch("vc_jobs.disk.shutil.disk_usage",
                        side_effect=OSError("нет доступа")):
            self.assertEqual(disk.free_bytes(self.root), 0)


class EnsureFreeTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcdisk_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_enough_space_passes(self):
        disk.ensure_free(self.root, 1, reserve_bytes=0)

    def test_insufficient_space_raises(self):
        with mock.patch("vc_jobs.disk.free_bytes", return_value=10):
            with self.assertRaises(disk.InsufficientSpace) as ctx:
                disk.ensure_free(self.root, 5, reserve_bytes=6)

        self.assertIn("Нет места", str(ctx.exception))

    def test_need_plus_reserve_is_compared(self):
        with mock.patch("vc_jobs.disk.free_bytes", return_value=10):
            disk.ensure_free(self.root, 5, reserve_bytes=5)
            with self.assertRaises(disk.InsufficientSpace):
                disk.ensure_free(self.root, 6, reserve_bytes=5)
