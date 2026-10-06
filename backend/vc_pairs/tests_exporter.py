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


class ExporterBoundaryTest(SimpleTestCase):
    """Точность границ нарезки: какие кадры реально попадают в файл.

    Каждый кадр исходника кодирует свой индекс пикселями (левая половина
    ``(i // 16) * 16``, правая ``(i % 16) * 16``; шаг 16 переживает lossy):
    выход декодируется покадрово и сверяется с ожидаемым [a, b] попиксельно,
    а не только счётчиком. Ловит сдвиги границ, которые счёт+первый кадр
    на статике не видят.
    """

    N = 120
    W, H = 64, 64

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден")
        super().setUpClass()
        import numpy as np

        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcbound_")
        cls.np = np
        cls.sources = {}
        for vfr in (False, True):
            path = os.path.join(cls.tmpdir.name, f"bound_vfr{vfr}.mp4")
            frames = []
            for i in range(cls.N):
                fr = np.zeros((cls.H, cls.W, 3), dtype=np.uint8)
                fr[:, : cls.W // 2] = (i // 16) * 16
                fr[:, cls.W // 2:] = (i % 16) * 16
                frames.append(fr)
            if vfr:
                half = cls.N // 2
                frames = frames[:half] + frames[half::2]
            raw = b"".join(f.tobytes() for f in frames)
            proc = subprocess.run(
                ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{cls.W}x{cls.H}", "-r", "25", "-i", "-",
                 "-vf", "format=yuv420p", "-c:v", "libx264", "-qp", "0",
                 "-preset", "ultrafast", "-g", "10", "-bf", "2",
                 "-vsync", "cfr", path],
                input=raw, capture_output=True)
            assert proc.returncode == 0, proc.stderr.decode()[-500:]
            cls.sources[vfr] = (path, len(frames))

    @classmethod
    def tearDownClass(cls):
        from vc_pairs import frame_provider as fp
        for path, _ in cls.sources.values():
            fp.close_source(path)
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def tearDown(self):
        from vc_pairs import frame_provider as fp
        for path, _ in self.sources.values():
            fp.close_source(path)

    def _infer(self, frame):
        """Индекс, зашитый в пиксели (None — поле побито beyond допуска)."""
        import cv2

        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(float)
        L = g[:, : self.W // 2].mean()
        R = g[:, self.W // 2:].mean()
        Lq = int(round(L / 16.0)) * 16
        Rq = int(round(R / 16.0)) * 16
        if abs(L - Lq) > 5 or abs(R - Rq) > 5:
            return None
        return (Lq // 16) * 16 + Rq // 16

    def _read_indices(self, path):
        import cv2

        cap = cv2.VideoCapture(path)
        try:
            out = []
            while True:
                ok, fr = cap.read()
                if not ok:
                    break
                out.append(self._infer(fr))
            return out
        finally:
            cap.release()

    def _check_range(self, vfr, a, b):
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        src, _ = self.sources[vfr]
        vpts = fp.get_visible_pts(src)
        self.assertTrue(0 <= a <= b < len(vpts))
        half = self.N // 2
        orig_of = (lambda i: i if i < half else half + 2 * (i - half)) \
            if vfr else (lambda i: i)
        out = tempfile.mkdtemp()
        try:
            created = Exporter(src, out).extract_fragments(
                [(a, b)],
                frame_ts_ranges=[_frame_ts_bounds(vpts, a, b)])
            self.assertEqual(len(created), 1)
            got = self._read_indices(created[0])
            self.assertEqual(
                got, [orig_of(i) for i in range(a, b + 1)],
                f"vfr={vfr} [{a},{b}]: в нарезке не те кадры: {got}")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_cfr_boundaries(self):
        """CFR: середина, одиночные (голова/середина/хвост), целиком."""
        for a, b in [(5, 8), (0, 0), (60, 60), (119, 119),
                     (0, 119), (117, 119), (0, 2)]:
            with self.subTest(fragment=(a, b)):
                self._check_range(False, a, b)

    def test_vfr_boundaries(self):
        """VFR через смену темпа: границы точны в видимых индексах."""
        for a, b in [(5, 8), (0, 0), (40, 40), (89, 89),
                     (0, 89), (87, 89), (0, 2)]:
            with self.subTest(fragment=(a, b)):
                self._check_range(True, a, b)


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


class FullVerifyTest(SimpleTestCase):
    """Обязательная полная сверка: каждый кадр + детект тихого сдвига."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe не найдены")
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcfull_")
        cls.path = os.path.join(cls.tmpdir.name, "dyn.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=160x120:rate=30:duration=3",
             "-c:v", "libx264", "-preset", "ultrafast",
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

    def _export(self, a, b):
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        vpts = fp.get_visible_pts(self.path)
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        created = Exporter(self.path, out).extract_fragments(
            [(a, b)], frame_ts_ranges=[_frame_ts_bounds(vpts, a, b)])
        return created[0]

    def test_full_verify_passes_exact_cut(self):
        """Точная нарезка проходит полную сверку молча."""
        import vc_fragments.views as export_views

        cut = self._export(10, 19)
        export_views._verify_cut(self.path, 10, cut, 10)

    def test_full_verify_catches_shift(self):
        """Срез [12,21], заявленный как [10,19]: именно сдвиг, не контент."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut = self._export(12, 21)
        with self.assertRaises(FFmpegError) as ctx:
            export_views._verify_cut(self.path, 10, cut, 10)
        self.assertIn("сдвиг", str(ctx.exception))
