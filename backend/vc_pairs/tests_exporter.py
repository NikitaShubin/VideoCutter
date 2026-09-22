# -*- coding: utf-8 -*-
"""Тесты мгновенной отмены экспорта (ядро): kill посреди фрагмента.

Прогон: cd backend && python manage.py test
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

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


class ExporterRangeTest(SimpleTestCase):
    def test_n_range_is_inclusive(self):
        """between(n,start,end) — ровно end-start+1 кадров, без +1."""
        from videocutter.core.exporter import Exporter

        cmd = Exporter("a.mp4", "/tmp").build_command(0, 3, "/tmp/x.mp4")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertEqual(
            vf, "select=between(n\\,0\\,3)")

    def test_frame_ts_range(self):
        """frame_ts_range: отбор по секундам, без счётчика n."""
        from videocutter.core.exporter import Exporter

        cmd = Exporter("a.mp4", "/tmp").build_command(
            0, 3, "/tmp/x.mp4", frame_ts_range=(1.0, 2.5))
        vf = cmd[cmd.index("-vf") + 1]
        self.assertEqual(
            vf, "select=between(t\\,1.0\\,2.5)")

    def test_frame_ts_ranges_length_mismatch(self):
        """frame_ts_ranges не той длины — явная ошибка, а не тихий рассинхрон."""
        from videocutter.core.exporter import Exporter
        from videocutter.core.exporter import FFmpegError

        exp = Exporter("a.mp4", "/tmp")
        with self.assertRaises(FFmpegError):
            exp.extract_fragments([(0, 3)], frame_ts_ranges=[(0, 1), (2, 3)])


def _ffprobe_count(path: str) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True)
    return int(out.stdout.strip())


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


class ExporterVfrTest(SimpleTestCase):
    """Roundtrip на VFR-клипе: привязка к меткам кадров, а не к счётчику n."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe не найдены")
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcvfr_")
        cls.path = os.path.join(cls.tmpdir.name, "vfr.mp4")
        # Первая половина — 30 fps целиком, вторая — каждый второй кадр:
        # метки рвутся (30 -> 15 fps), -vsync vfr сохраняет исходные pts.
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=96x96:rate=30:duration=4",
             "-vf", "select='lt(t,2)+gte(t,2)*not(mod(n,2))'",
             "-vsync", "vfr", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", cls.path],
            check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        from vc_pairs import frame_provider as fp
        fp.close_source(cls.path)
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def tearDown(self):
        from vc_pairs import frame_provider as fp
        fp.close_source(self.path)

    def test_vfr_roundtrip_spanning_rate_change(self):
        """Срез через смену темпа: ровно b-a+1 кадров и верная длительность."""
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        vpts = fp.get_visible_pts(self.path)
        gaps = {vpts[i + 1] - vpts[i] for i in range(len(vpts) - 1)}
        self.assertGreater(len(gaps), 1, "клип не VFR: все межкадровые равны")
        # Диапазон через смену темпа (метка 2с — внутри диапазона).
        cross = next(i for i, t in enumerate(vpts) if t >= 2_000_000)
        a = max(0, cross - 5)
        b = min(len(vpts) - 1, cross + 15)
        bounds = _frame_ts_bounds(vpts, a, b)
        out = tempfile.mkdtemp()
        try:
            created = Exporter(self.path, out).extract_fragments(
                [(a, b)], frame_ts_ranges=[bounds])
            self.assertEqual(len(created), 1)
            self.assertEqual(_ffprobe_count(created[0]), b - a + 1)
            expect_dur = (vpts[b] - vpts[a]) / 1e6
            self.assertAlmostEqual(
                _ffprobe_duration(created[0]), expect_dur, delta=0.15)
        finally:
            shutil.rmtree(out, ignore_errors=True)
