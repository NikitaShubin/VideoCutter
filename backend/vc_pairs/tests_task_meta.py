# -*- coding: utf-8 -*-
"""Тесты task.json (task_meta): дефолты, чтение, запись, валидация.

Прогон:  cd backend && python manage.py test vc_pairs
"""

import json
import os
import tempfile

from django.test import SimpleTestCase

import task_meta


class TaskMetaTests(SimpleTestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_raw(self, text: str) -> None:
        with open(os.path.join(self._tmpdir, task_meta.TASK_FILE), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def test_load_missing_returns_defaults(self):
        meta = task_meta.load(self._tmpdir)
        self.assertEqual(meta["status"], "new")
        self.assertEqual(meta["stage"], "annotation")
        self.assertEqual(meta["assignees"], [])
        self.assertIsNone(meta["owner"])

    def test_load_broken_returns_defaults(self):
        for bad in ("{не json", "[1, 2]",
                    '{"status": "нет-такого", "stage": "annotation"}'):
            self._write_raw(bad)
            meta = task_meta.load(self._tmpdir)
            self.assertEqual(meta["status"], "new", bad)
            self.assertEqual(meta["stage"], "annotation", bad)

    def test_load_partial_fills_and_keeps_unknown(self):
        self._write_raw('{"owner": "ivan", "assignees": ["ivan"], '
                        '"status": "in_progress", "future_key": 42}')
        meta = task_meta.load(self._tmpdir)
        self.assertEqual(meta["owner"], "ivan")
        self.assertEqual(meta["assignees"], ["ivan"])
        self.assertEqual(meta["status"], "in_progress")
        self.assertEqual(meta["stage"], "annotation")
        self.assertEqual(meta["future_key"], 42)

    def test_save_roundtrip_atomic(self):
        meta = task_meta.init_new("ivan")
        task_meta.set_status(meta, "in_progress", by="ivan")
        meta["assignees"] = ["ivan", "petr"]
        saved = task_meta.save(self._tmpdir, meta)
        self.assertEqual(saved["status"], "in_progress")
        self.assertIsNotNone(saved["status_changed_at"])
        # Временный файл не остаётся, итог читается обратно 1-в-1.
        self.assertFalse(os.path.exists(
            os.path.join(self._tmpdir, task_meta.TASK_FILE + ".tmp")))
        back = task_meta.load(self._tmpdir)
        self.assertEqual(back, saved)

    def test_save_rejects_bad_enum(self):
        for field, bad in (("status", "assigned"), ("stage", "review"),
                           ("subset", "prod")):
            meta = task_meta.defaults()
            meta[field] = bad
            with self.assertRaises(ValueError, msg=f"{field}={bad}"):
                task_meta.save(self._tmpdir, meta)
        self.assertFalse(os.path.exists(
            os.path.join(self._tmpdir, task_meta.TASK_FILE)))

    def test_set_status_rejects_bad(self):
        with self.assertRaises(ValueError):
            task_meta.set_status(task_meta.defaults(), "assigned")
