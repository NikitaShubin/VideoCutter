# -*- coding: utf-8 -*-
"""Журнал активности: JSONL, фильтры, чистка, ссылки.

Прогон: cd backend && python manage.py test vc_jobs.tests_activity
"""

import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import SimpleTestCase

from vc_jobs.activity import ActivityLog, target_link


class ActivityLogTests(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcactivity_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.log = ActivityLog(self.root, retention_days=90)

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_append_writes_world_readable_jsonl_with_link(self):
        event = self.log.append(
            "job.done", actor="alice",
            target={"kind": "task", "id": "camera_8"},
            project_id="p1", job_id="j1", summary="33 нарезки")

        day = event["ts"][:10]
        path = os.path.join(self.root, day + ".jsonl")
        self.assertEqual(self._mode(path), 0o644)
        with open(path, encoding="utf-8") as f:
            saved = json.loads(f.readline())
        self.assertEqual(saved["action"], "job.done")
        self.assertEqual(saved["actor"], "alice")
        self.assertEqual(saved["link"], "/tasks/camera_8")
        self.assertEqual(saved["project_id"], "p1")
        self.assertEqual(saved["job_id"], "j1")

    def test_explicit_link_and_details_are_preserved(self):
        event = self.log.append(
            "task.opened", target={"kind": "project", "id": "p1"},
            link="/custom", details={"frame": 10})

        self.assertEqual(event["link"], "/custom")
        self.assertEqual(event["details"], {"frame": 10})

    def test_append_survives_chmod_error(self):
        with mock.patch("vc_jobs.activity.os.chmod",
                        side_effect=OSError("ro")):
            event = self.log.append("job.created")

        self.assertEqual(event["action"], "job.created")

    def test_read_returns_newest_first(self):
        self.log.append("one", ts="2026-01-01T10:00:00+00:00")
        self.log.append("two", ts="2026-01-01T11:00:00+00:00")

        events = self.log.read()

        self.assertEqual([e["action"] for e in events], ["two", "one"])

    def test_read_filters_by_time_project_and_task(self):
        self.log.append("a", ts="2026-01-01T10:00:00+00:00",
                        target={"kind": "task", "id": "t1"}, project_id="p1")
        self.log.append("b", ts="2026-01-02T10:00:00+00:00",
                        target={"kind": "task", "id": "t2"}, project_id="p2")
        self.log.append("c", ts="2026-01-03T10:00:00+00:00",
                        target={"kind": "project", "id": "p1"},
                        project_id="p1")

        self.assertEqual([e["action"] for e in self.log.read(
            from_="2026-01-02T00:00:00+00:00")], ["c", "b"])
        self.assertEqual([e["action"] for e in self.log.read(
            to="2026-01-02T23:59:59+00:00")], ["b", "a"])
        self.assertEqual([e["action"] for e in self.log.read(
            project_id="p1")], ["c", "a"])
        self.assertEqual([e["action"] for e in self.log.read(
            task_id="t1")], ["a"])

    def test_read_accepts_datetime_bounds_and_limit(self):
        self.log.append("a", ts="2026-01-01T10:00:00+00:00")
        self.log.append("b", ts="2026-01-02T10:00:00+00:00")

        events = self.log.read(
            from_=datetime(2026, 1, 1, tzinfo=timezone.utc),
            to=datetime(2026, 1, 3), limit=1)

        self.assertEqual([e["action"] for e in events], ["b"])

    def test_read_skips_corrupt_lines_and_bad_timestamps(self):
        path = os.path.join(self.root, "2026-01-01.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken\n")
            f.write(json.dumps({"action": "bad-time", "ts": "nope"}) + "\n")
            f.write(json.dumps({"action": "ok",
                                "ts": "2026-01-01T00:00:00+00:00"}) + "\n")
            f.write(json.dumps(["not", "an", "object"]) + "\n")

        events = self.log.read()

        self.assertEqual([e["action"] for e in events], ["ok"])

    def test_read_missing_directory_is_empty(self):
        shutil.rmtree(self.root)

        self.assertEqual(self.log.read(), [])

    def test_read_skips_unreadable_file(self):
        self.log.append("a", ts="2026-01-01T00:00:00+00:00")

        with mock.patch("builtins.open", side_effect=OSError("denied")):
            self.assertEqual(self.log.read(), [])

    def test_prune_removes_only_files_older_than_retention(self):
        old = os.path.join(self.root, "2025-01-01.jsonl")
        fresh = os.path.join(self.root, "2025-04-01.jsonl")
        noise = os.path.join(self.root, "readme.txt")
        for path in (old, fresh, noise):
            with open(path, "w") as f:
                f.write("x")
        now = datetime(2025, 4, 10, tzinfo=timezone.utc)

        removed = self.log.prune(now=now)

        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(noise))

    def test_prune_skips_bad_names_and_unlink_errors(self):
        bad = os.path.join(self.root, "not-a-date.jsonl")
        with open(bad, "w") as f:
            f.write("x")
        old = os.path.join(self.root, "2020-01-01.jsonl")
        with open(old, "w") as f:
            f.write("x")

        with mock.patch("vc_jobs.activity.os.unlink", side_effect=OSError):
            removed = self.log.prune(now=datetime(2026, 1, 1,
                                                 tzinfo=timezone.utc))

        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(bad))
        self.assertTrue(os.path.exists(old))

    def test_prune_missing_directory_returns_zero(self):
        shutil.rmtree(self.root)

        self.assertEqual(self.log.prune(), 0)

    def test_target_link_variants(self):
        self.assertEqual(target_link({"kind": "task", "id": "t"}),
                         "/tasks/t")
        self.assertEqual(target_link({"kind": "project", "id": "p"}),
                         "/projects/p")
        self.assertIsNone(target_link({"kind": "other", "id": "x"}))
        self.assertIsNone(target_link({"kind": "task"}))
        self.assertIsNone(target_link(None))
