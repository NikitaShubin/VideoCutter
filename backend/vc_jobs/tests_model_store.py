# -*- coding: utf-8 -*-
"""Модель записи операции и файловое хранилище.

Прогон: cd backend && python manage.py test vc_jobs.tests_model_store
"""

import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from vc_jobs.model import Job, JobValidationError, parse_iso
from vc_jobs.store import JobStore


def make_job(**kw):
    data = dict(
        id="0f0c0f0c-1111-2222-3333-444455556666",
        type="export.fragments",
        lane="export",
        status="queued",
        dedupe_key="export.fragments:task:t1:owner:local",
        owner="local",
        target={"kind": "task", "id": "t1", "project_id": None},
    )
    data.update(kw)
    return Job(**data)


class JobModelTests(SimpleTestCase):
    def test_defaults_are_a_queued_job(self):
        job = make_job()

        self.assertEqual(job.status, "queued")
        self.assertEqual(job.priority, 0)
        self.assertEqual(job.attempt, 1)
        self.assertEqual(job.progress["current"], 0)
        self.assertIsNone(job.result)
        self.assertIsNone(job.error)
        self.assertFalse(job.cancel_requested)
        self.assertTrue(job.created_at)

    def test_active_and_terminal_flags(self):
        self.assertTrue(make_job(status="queued").active)
        self.assertTrue(make_job(status="running").active)
        self.assertFalse(make_job(status="done").active)
        self.assertTrue(make_job(status="done").terminal)
        self.assertTrue(make_job(status="error").terminal)
        self.assertTrue(make_job(status="cancelled").terminal)
        self.assertTrue(make_job(status="interrupted").terminal)

    def test_roundtrip_preserves_fields_and_extra_keys(self):
        job = make_job(params={"format": "mp4"}, priority=100,
                       depends_on=["a"], extra={"future": 1})

        restored = Job.from_dict(job.to_dict())

        self.assertEqual(restored.to_dict(), job.to_dict())
        self.assertEqual(restored.extra, {"future": 1})

    def test_unknown_keys_from_future_version_survive_roundtrip(self):
        data = make_job().to_dict()
        data["future_field"] = {"x": 1}

        restored = Job.from_dict(data)

        self.assertEqual(restored.to_dict()["future_field"], {"x": 1})

    def test_extra_cannot_override_core_fields(self):
        job = make_job(extra={"status": "done", "id": "hacked"})

        self.assertEqual(job.to_dict()["status"], "queued")
        self.assertEqual(job.to_dict()["id"], job.id)

    def test_cancelling_is_computed_not_stored(self):
        job = make_job(status="running", cancel_requested=True)

        self.assertEqual(job.display_status, "cancelling")
        self.assertEqual(job.to_dict()["status"], "running")

    def test_display_status_keeps_terminal_states(self):
        job = make_job(status="cancelled", cancel_requested=True)

        self.assertEqual(job.display_status, "cancelled")

    def test_invalid_status_rejected(self):
        with self.assertRaises(JobValidationError):
            Job.from_dict({**make_job().to_dict(), "status": "weird"})

    def test_missing_required_field_rejected(self):
        data = make_job().to_dict()
        data.pop("owner")

        with self.assertRaises(JobValidationError):
            Job.from_dict(data)

    def test_non_dict_target_rejected(self):
        data = make_job().to_dict()
        data["target"] = "task"

        with self.assertRaises(JobValidationError):
            Job.from_dict(data)

    def test_target_requires_kind_and_id(self):
        data = make_job().to_dict()
        data["target"] = {"kind": "task"}

        with self.assertRaises(JobValidationError):
            Job.from_dict(data)

    def test_progress_clamps_negative_and_over_total(self):
        job = make_job()

        job.set_progress(-1, 10)
        self.assertEqual(job.progress["current"], 0)
        job.set_progress(99, 10)
        self.assertEqual(job.progress["current"], 10)
        self.assertEqual(job.progress["total"], 10)

    def test_progress_message_and_unit(self):
        job = make_job()

        job.set_progress(2, 5, "режу", unit="fragment")

        self.assertEqual(job.progress,
                         {"current": 2, "total": 5, "unit": "fragment",
                          "message": "режу"})

    def test_touch_heartbeat_sets_timestamp(self):
        job = make_job(status="running")

        job.touch()

        self.assertTrue(job.heartbeat_at)

    def test_naive_timestamp_is_treated_as_utc(self):
        parsed = parse_iso("2026-01-01T12:00:00")

        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 12)


class JobStoreTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcjobs_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = JobStore(self.root)

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_save_and_get_roundtrip(self):
        job = make_job()

        self.store.save(job)

        self.assertEqual(self.store.get(job.id).to_dict(), job.to_dict())

    def test_saved_file_is_world_readable_json(self):
        job = make_job()

        self.store.save(job)
        path = os.path.join(self.root, job.id + ".json")

        self.assertEqual(self._mode(path), 0o644)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["id"], job.id)

    def test_save_overwrites_atomically_without_leftover_tmp(self):
        job = make_job()
        self.store.save(job)
        job.status = "done"

        self.store.save(job)

        self.assertEqual(self.store.get(job.id).status, "done")
        self.assertEqual(
            [n for n in os.listdir(self.root) if n.endswith(".tmp")], [])

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get(make_job().id))

    def test_get_rejects_path_traversal(self):
        self.assertIsNone(self.store.get("../../etc/passwd"))
        self.assertIsNone(self.store.get(""))

    def test_corrupt_file_is_skipped_not_fatal(self):
        job = make_job()
        self.store.save(job)
        with open(os.path.join(self.root, job.id + ".json"), "w") as f:
            f.write("{broken")

        self.assertIsNone(self.store.get(job.id))
        self.assertEqual(self.store.all(), [])

    def test_all_sorted_by_created_at(self):
        older = make_job(id="a" * 36,
                         created_at="2026-01-01T00:00:00+00:00")
        newer = make_job(id="b" * 36,
                         created_at="2026-01-02T00:00:00+00:00")
        self.store.save(newer)
        self.store.save(older)

        self.assertEqual([j.id for j in self.store.all()],
                         ["a" * 36, "b" * 36])

    def test_delete_removes_record(self):
        job = make_job()
        self.store.save(job)

        self.store.delete(job.id)

        self.assertIsNone(self.store.get(job.id))

    def test_delete_missing_is_noop(self):
        self.store.delete(make_job().id)

    def test_prune_removes_only_old_terminal_jobs(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=40)).isoformat()
        fresh = now.isoformat()
        done_old = make_job(id="a" * 36, status="done", finished_at=old)
        done_fresh = make_job(id="b" * 36, status="done", finished_at=fresh)
        running_old = make_job(id="c" * 36, status="running", created_at=old)
        no_finish = make_job(id="d" * 36, status="error")
        for job in (done_old, done_fresh, running_old, no_finish):
            self.store.save(job)

        removed = self.store.prune(before=now - timedelta(days=30))

        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get(done_old.id))
        self.assertIsNotNone(self.store.get(done_fresh.id))
        self.assertIsNotNone(self.store.get(running_old.id))
        self.assertIsNotNone(self.store.get(no_finish.id))

    def test_prune_survives_unlink_error(self):
        now = datetime.now(timezone.utc)
        job = make_job(id="a" * 36, status="done",
                       finished_at=(now - timedelta(days=40)).isoformat())
        self.store.save(job)

        from unittest import mock
        with mock.patch("vc_jobs.store.os.unlink", side_effect=OSError("ro")):
            removed = self.store.prune(before=now - timedelta(days=30))

        self.assertEqual(removed, 0)

    def test_save_oserror_does_not_leave_tmp(self):
        from unittest import mock
        job = make_job()

        with mock.patch("vc_jobs.store.os.replace", side_effect=OSError("ro")):
            with self.assertRaises(OSError):
                self.store.save(job)

        self.assertEqual(
            [n for n in os.listdir(self.root) if n.endswith(".tmp")], [])

    def test_save_oserror_with_unremovable_tmp_still_raises(self):
        from unittest import mock
        job = make_job()

        with mock.patch("vc_jobs.store.os.replace", side_effect=OSError("ro")), \
                mock.patch("vc_jobs.store.os.unlink", side_effect=OSError("ro")):
            with self.assertRaises(OSError):
                self.store.save(job)

    def test_save_rejects_invalid_id(self):
        with self.assertRaises(ValueError):
            self.store.save(make_job(id="../escape"))

    def test_delete_invalid_id_is_noop(self):
        self.store.delete("../escape")

    def test_all_ignores_non_json_and_directories(self):
        os.makedirs(os.path.join(self.root, "subdir"))
        with open(os.path.join(self.root, "note.txt"), "w") as f:
            f.write("x")

        self.assertEqual(self.store.all(), [])
