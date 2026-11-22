# -*- coding: utf-8 -*-
"""Контрольные точки проверки: отпечаток, слияние ролей, чистка."""

import json
import os
import shutil
import tempfile
import time
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs import checkpoints


class CheckpointTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcpoint_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.state = os.path.join(self.root, "state")
        self.env = mock.patch.dict(
            os.environ, {"VC_STATE_ROOT": self.state})
        self.env.start()
        self.addCleanup(self.env.stop)

    def files(self):
        try:
            return sorted(os.listdir(checkpoints.root()))
        except OSError:
            return []


class RoundTripTests(CheckpointTestBase):
    def test_saved_role_is_loaded_for_same_fingerprint(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})

        self.assertEqual(
            checkpoints.load("task", "fp"),
            {"source": {"total_frames": 10}})

    def test_other_fingerprint_or_task_is_empty(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})

        self.assertEqual(checkpoints.load("task", "other"), {})
        self.assertEqual(checkpoints.load("other", "fp"), {})

    def test_roles_are_merged(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        checkpoints.save_role("task", "fp", "preview", {"total_frames": 10})

        self.assertEqual(set(checkpoints.load("task", "fp")),
                         {"source", "preview"})

    def test_non_dict_metadata_is_ignored(self):
        checkpoints.save_role("task", "fp", "source", "oops")

        self.assertEqual(checkpoints.load("task", "fp"), {})
        self.assertEqual(self.files(), [])

    def test_clear_removes_only_this_version(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        checkpoints.save_role("task", "other", "source", {"total_frames": 10})

        checkpoints.clear("task", "fp")

        self.assertEqual(checkpoints.load("task", "fp"), {})
        self.assertTrue(checkpoints.load("task", "other"))

    def test_clear_missing_file_is_noop(self):
        checkpoints.clear("task", "nope")

    def test_clear_all_removes_everything(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})

        checkpoints.clear_all()

        self.assertEqual(self.files(), [])

    def test_clear_all_missing_directory_is_noop(self):
        checkpoints.clear_all()


class CorruptFileTests(CheckpointTestBase):
    def _path(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        return os.path.join(checkpoints.root(), self.files()[0])

    def test_broken_json_is_empty(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")

        self.assertEqual(checkpoints.load("task", "fp"), {})

    def test_non_object_json_is_empty(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]")

        self.assertEqual(checkpoints.load("task", "fp"), {})

    def test_roles_must_be_object(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "task", "fingerprint": "fp",
                       "roles": []}, fh)

        self.assertEqual(checkpoints.load("task", "fp"), {})

    def test_non_dict_role_is_filtered(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"task_id": "task", "fingerprint": "fp",
                       "roles": {"source": [], "preview": {"fps": 25}}}, fh)

        self.assertEqual(checkpoints.load("task", "fp"),
                         {"preview": {"fps": 25}})

    def test_save_over_corrupt_file_starts_fresh(self):
        path = self._path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")

        checkpoints.save_role("task", "fp", "preview", {"fps": 25})

        self.assertEqual(checkpoints.load("task", "fp"),
                         {"preview": {"fps": 25}})

    def test_save_swallows_write_error(self):
        path = self._path()
        with mock.patch("vc_jobs.checkpoints.os.replace",
                        side_effect=OSError("ro")):
            checkpoints.save_role("task", "fp", "preview", {"fps": 25})

        self.assertEqual(checkpoints.load("task", "fp"),
                         {"source": {"total_frames": 10}})
        self.assertTrue(os.path.isfile(path))

    def test_save_removes_temporary_file_on_error(self):
        with mock.patch("vc_jobs.checkpoints.os.replace",
                        side_effect=OSError("ro")):
            checkpoints.save_role("task", "fp", "source", {"fps": 25})

        self.assertEqual(
            [name for name in self.files() if name.startswith(".tmp-")], [])


class PruneTests(CheckpointTestBase):
    def test_prune_removes_old_points_and_temp_files(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        directory = checkpoints.root()
        old = os.path.join(directory, self.files()[0])
        temp = os.path.join(directory, ".tmp-old")
        with open(temp, "w", encoding="utf-8") as fh:
            fh.write("x")
        stamp = time.time() - 8 * 86400
        os.utime(old, (stamp, stamp))
        os.utime(temp, (stamp, stamp))

        removed = checkpoints.prune()

        self.assertEqual(removed, 2)
        self.assertEqual(self.files(), [])

    def test_prune_keeps_fresh_and_foreign_files(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        foreign = os.path.join(checkpoints.root(), "notes.txt")
        with open(foreign, "w", encoding="utf-8") as fh:
            fh.write("x")

        removed = checkpoints.prune()

        self.assertEqual(removed, 0)
        self.assertIn("notes.txt", self.files())

    def test_prune_missing_directory_is_noop(self):
        self.assertEqual(checkpoints.prune(), 0)

    def test_prune_survives_unremovable_file(self):
        checkpoints.save_role("task", "fp", "source", {"total_frames": 10})
        old = os.path.join(checkpoints.root(), self.files()[0])
        stamp = time.time() - 8 * 86400
        os.utime(old, (stamp, stamp))

        with mock.patch("vc_jobs.checkpoints.os.remove",
                        side_effect=OSError("ro")):
            removed = checkpoints.prune()

        self.assertEqual(removed, 0)
