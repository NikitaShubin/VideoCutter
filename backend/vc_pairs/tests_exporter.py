# -*- coding: utf-8 -*-
"""Тесты мгновенной отмены экспорта (ядро): kill посреди фрагмента.

Прогон: cd backend && python manage.py test
"""

import os
import shutil
import tempfile
import time

from django.test import SimpleTestCase

from videocutter.core.exporter import ExportCancelled, Exporter


class ExporterCancelTest(SimpleTestCase):
    def test_cancelled_mid_fragment(self):
        """cancelled()=True сразу: ExportCancelled за секунды, без висения."""
        src = os.path.join(
            os.path.dirname(__file__), "..", "testdata", "test.mp4")
        out = tempfile.mkdtemp()
        try:
            exp = Exporter(src, out)
            t0 = time.time()
            with self.assertRaises(ExportCancelled):
                exp.extract_fragments([(0, 9)], cancelled=lambda: True)
            self.assertLess(time.time() - t0, 20)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_no_cancel_runs_through(self):
        """Без отмены — обычный прогон (контрпример, регрессия Popen-цикла)."""
        src = os.path.join(
            os.path.dirname(__file__), "..", "testdata", "test.mp4")
        out = tempfile.mkdtemp()
        try:
            exp = Exporter(src, out)
            created = exp.extract_fragments(
                [(0, 9)], cancelled=lambda: False)
            self.assertEqual(len(created), 1)
            self.assertTrue(os.path.isfile(created[0]))
        finally:
            shutil.rmtree(out, ignore_errors=True)
