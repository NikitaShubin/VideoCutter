# -*- coding: utf-8 -*-
"""Тесты мгновенной отмены экспорта (ядро): kill посреди фрагмента.

Прогон: cd backend && python manage.py test
"""

import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

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


class ExporterPublishTest(SimpleTestCase):
    """Частичный файл скрыт и 0600; публикация — атомарный rename + 0644."""

    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)
        src = os.path.join(
            os.path.dirname(__file__), "..", "testdata", "test.mp4")
        self.exporter = Exporter(src, self.out)

    def test_partial_hidden_and_private_until_published(self):
        partial = self.exporter.extract_fragments([(0, 9)])[0]
        self.assertTrue(os.path.basename(partial).startswith("."))
        self.assertEqual(stat.S_IMODE(os.stat(partial).st_mode), 0o600)
        self.assertFalse(
            os.path.exists(os.path.join(self.out, "test_fragment_1.mp4")))

        final = self.exporter.publish(partial)
        self.assertEqual(os.path.basename(final), "test_fragment_1.mp4")
        self.assertEqual(stat.S_IMODE(os.stat(final).st_mode), 0o644)
        self.assertFalse(os.path.exists(partial))


class ExporterRangeTest(SimpleTestCase):
    def test_n_range_is_inclusive(self):
        """between(n,start,end) — ровно end-start+1 кадров, без +1."""
        from videocutter.core.exporter import Exporter

        cmd = Exporter("a.mp4", "/tmp").build_command(0, 3, "/tmp/x.mp4")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertEqual(
            vf, "select=between(n\\,0\\,3)")

    def test_video_command_caps_output_frames(self):
        """Видео: -frames:v = ожидаемому числу кадров (не +1)."""
        from videocutter.core.exporter import Exporter

        cmd = Exporter("a.mp4", "/tmp").build_command(10, 19, "/tmp/x.mp4")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "10")

    def test_png_command_keeps_single_frame_cap(self):
        """PNG по-прежнему ровно один кадр."""
        from videocutter.core.exporter import Exporter

        cmd = Exporter("a.mp4", "/tmp").build_command(10, 10, "/tmp/x.png")
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")

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

    def test_target_suffix_fallback_mp4(self):
        """Контейнеры без H.264 (webm/ogv): суффикс .mp4, пайплайн тот же."""
        from videocutter.core.exporter import Exporter

        self.assertTrue(
            Exporter("a.webm", "/tmp").target_suffix == ".mp4")
        self.assertTrue(
            Exporter("a.ogv", "/tmp").target_suffix == ".mp4")
        self.assertTrue(
            Exporter("a.mp4", "/tmp").target_suffix == ".mp4")
        self.assertTrue(
            Exporter("a.mpg", "/tmp").target_suffix == ".mpg")
        self.assertTrue(
            Exporter("a.avi", "/tmp").target_suffix == ".avi")


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

    def test_vfr_cut_passes_full_verification(self):
        """Срез через смену темпа проходит сверку без дублей кадров."""
        import vc_fragments.views as export_views
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        vpts = fp.get_visible_pts(self.path)
        cross = next(i for i, t in enumerate(vpts) if t >= 2_000_000)
        a = max(0, cross - 5)
        b = min(len(vpts) - 1, cross + 15)
        out = tempfile.mkdtemp()
        try:
            created = Exporter(self.path, out).extract_fragments(
                [(a, b)], frame_ts_ranges=[_frame_ts_bounds(vpts, a, b)])
            self.assertEqual(len(created), 1)
            export_views._verify_cut(self.path, a, created[0], b - a + 1)
        finally:
            shutil.rmtree(out, ignore_errors=True)


class StartOffsetTest(SimpleTestCase):
    """Ненулевой start_time: шкала индекса нормализована, экспорт точен.

    Регресс класса «тихий сдвиг» (MPEG-PS/FLV): сырой PTS идёт от
    start_time, а ffmpeg-select считает от нуля. Без нормализации все
    границы уезжали бы на start_time (ошибка сверки или молча чужие
    кадры — по статике/динамике).
    """

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe не найдены")
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcoff_")
        base = os.path.join(cls.tmpdir.name, "base.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=160x120:rate=25:duration=3",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", base],
            check=True, capture_output=True)
        cls.path = os.path.join(cls.tmpdir.name, "offset.mpg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", base, "-output_ts_offset", "0.55",
             "-c:v", "mpeg2video", "-q:v", "2", "-pix_fmt", "yuv420p",
             cls.path],
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

    def test_visible_pts_start_at_zero(self):
        """Индекс в шкале ffmpeg (от нуля), а не в сыром PTS."""
        from vc_pairs import frame_provider as fp

        vpts = fp.get_visible_pts(self.path)
        self.assertLess(vpts[0], 50000, f"шкала не нормализована: {vpts[0]}")

    def test_export_on_offset_file_verifies(self):
        """Экспорт середины со сдвинутого файла проходит полную сверку."""
        import vc_fragments.views as export_views
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        vpts = fp.get_visible_pts(self.path)
        a, b = 2, min(12, len(vpts) - 1)
        self.assertLess(a, b)
        out = tempfile.mkdtemp()
        try:
            created = Exporter(self.path, out).extract_fragments(
                [(a, b)], frame_ts_ranges=[_frame_ts_bounds(vpts, a, b)])
            self.assertEqual(len(created), 1)
            export_views._verify_cut(self.path, a, created[0], b - a + 1)
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

    def test_full_verify_single_frame_has_no_neighbours(self):
        """Один кадр: детект сдвига не ищет соседей (регресс min() пустого).

        Сжатие crf20 даёт ssd заметно выше 1, но ниже порога контента:
        прежний код уходил в поиск соседей и падал ValueError.
        """
        import vc_fragments.views as export_views
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        cut = os.path.join(out, "one.mp4")
        t0, t1 = _frame_ts_bounds(fp.get_visible_pts(self.path), 10, 10)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", self.path, "-y",
             "-vf", f"select=between(t\\,{t0}\\,{t1})", "-an", "-vsync", "0",
             "-c:v", "libx264", "-preset", "slow", "-crf", "20",
             "-tune", "animation", cut],
            check=True, capture_output=True)
        export_views._verify_cut(self.path, 10, cut, 1)

    def test_single_frame_exports_png(self):
        """Один кадр (start == end) — PNG с ровно одним кадром."""
        cut = self._export(10, 10)
        self.assertEqual(os.path.splitext(cut)[1], ".png")
        self.assertEqual(_ffprobe_count(cut), 1)

    def test_multi_frame_stays_video(self):
        """Несколько кадров — по-прежнему видео, не PNG."""
        cut = self._export(10, 11)
        self.assertEqual(os.path.splitext(cut)[1], ".mp4")


class SinglePassVerifyTest(SimpleTestCase):
    """Сверка набора фрагментов: один секвентальный проход по исходнику."""

    N = 200

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise unittest.SkipTest("ffmpeg/ffprobe не найдены")
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcsp_")
        cls.path = os.path.join(cls.tmpdir.name, "sp.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"testsrc2=size=96x96:rate=25:duration=8",
             "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2",
             "-g", "10", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             cls.path],
            check=True, capture_output=True)
        assert _ffprobe_count(cls.path) == cls.N

    @classmethod
    def tearDownClass(cls):
        from vc_pairs import frame_provider as fp
        fp.close_source(cls.path)
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def tearDown(self):
        from vc_pairs import frame_provider as fp
        fp.close_source(self.path)

    def _export(self, a, b, src=None):
        from vc_fragments.views import _frame_ts_bounds
        from vc_pairs import frame_provider as fp

        src = src or self.path
        vpts = fp.get_visible_pts(src)
        out = tempfile.mkdtemp(dir=self.tmpdir.name)
        created = Exporter(src, out).extract_fragments(
            [(a, b)], frame_ts_ranges=[_frame_ts_bounds(vpts, a, b)])
        self.assertEqual(len(created), 1)
        return created[0]

    def test_batch_accepts_multiple_exact_fragments(self):
        """Два не пересекающихся точных фрагмента — сверка проходит."""
        import vc_fragments.views as export_views

        cut1 = self._export(10, 19)
        cut2 = self._export(100, 109)
        export_views._verify_export_cuts(
            self.path, [(10, 19, cut1), (100, 109, cut2)])

    def test_batch_catches_shift_in_later_fragment(self):
        """Сдвиг во втором фрагменте ловится, путь нарезки — в ошибке."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut1 = self._export(10, 19)
        cut2 = self._export(101, 110)
        with self.assertRaises(FFmpegError) as ctx:
            export_views._verify_export_cuts(
                self.path, [(10, 19, cut1), (100, 109, cut2)])
        self.assertIn("сдвиг", str(ctx.exception))
        self.assertIn(cut2, str(ctx.exception))

    def test_batch_matches_per_fragment_verification(self):
        """Вердикт batch совпадает с независимой проверкой того же среза."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut = self._export(101, 110)
        with self.assertRaises(FFmpegError):
            export_views._verify_cut(self.path, 100, cut, 10)
        with self.assertRaises(FFmpegError):
            export_views._verify_export_cuts(
                self.path, [(100, 109, cut)])

    def test_source_decoded_once_for_all_fragments(self):
        """Исходник декодируется одним процессом, без seek (-ss)."""
        import vc_fragments.views as export_views

        cut1 = self._export(10, 19)
        cut2 = self._export(100, 109)
        real_popen = subprocess.Popen
        calls = []

        def counting_popen(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return real_popen(cmd, *args, **kwargs)

        with mock.patch.object(
                export_views.subprocess, "Popen", side_effect=counting_popen):
            export_views._verify_export_cuts(
                self.path, [(10, 19, cut1), (100, 109, cut2)])

        source_calls = [
            cmd for cmd in calls
            if self.path in cmd and "rawvideo" in cmd]
        self.assertEqual(len(source_calls), 1)
        self.assertNotIn("-ss", source_calls[0])

    def test_overlapping_fragments_verify_independently(self):
        """Пересечение (аномалия) — независимая сверка, а не общий проход."""
        import vc_fragments.views as export_views

        cut1 = self._export(10, 19)
        cut2 = self._export(15, 24)
        real_verify = export_views._verify_cut
        calls = []

        def counting_verify(src, start, cut, expected):
            calls.append((start, expected))
            return real_verify(src, start, cut, expected)

        with mock.patch.object(
                export_views, "_verify_cut", side_effect=counting_verify):
            export_views._verify_export_cuts(
                self.path, [(10, 19, cut1), (15, 24, cut2)])
        self.assertEqual(calls, [(10, 10), (15, 10)])

    def test_truncated_source_verifies_earlier_and_fails_later(self):
        """Обрыв хвоста исходника не отменяет сверку ранних фрагментов."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        src = os.path.join(self.tmpdir.name, "trunc.mp4")
        shutil.copy2(self.path, src)
        cut_before = self._export(0, 9, src=src)
        cut_after = self._export(180, 189, src=src)
        size = os.path.getsize(src)
        with open(src, "r+b") as f:
            f.truncate(size // 2)

        export_views._verify_export_cuts(src, [(0, 9, cut_before)])
        with self.assertRaises(FFmpegError) as ctx:
            export_views._verify_export_cuts(
                src, [(180, 189, cut_after)])
        self.assertIn("эталон", str(ctx.exception))

    def test_cancel_during_source_pass_stops_promptly(self):
        """Отмена в проходе по исходнику: быстро, без мусорного temp-файла."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import ExportCancelled

        cut = self._export(10, 19)
        calls = []
        temp_paths = []
        real_mkstemp = tempfile.mkstemp

        def mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            temp_paths.append(path)
            return fd, path

        def cancelled():
            calls.append(1)
            return len(calls) >= 2

        with mock.patch.object(
                export_views.tempfile, "mkstemp", side_effect=mkstemp):
            t0 = time.time()
            with self.assertRaises(ExportCancelled):
                export_views._verify_export_cuts(
                    self.path, [(10, 19, cut)], cancelled=cancelled)
            self.assertLess(time.time() - t0, 5)
        self.assertFalse(os.path.exists(temp_paths[0]))

    def test_batch_rejects_foreign_content(self):
        """Чужой диапазон при верном счётчике — ошибка контента."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut = self._export(120, 129)
        with self.assertRaises(FFmpegError) as ctx:
            export_views._verify_export_cuts(
                self.path, [(100, 109, cut)])
        self.assertIn("Контент", str(ctx.exception))

    def test_batch_does_not_materialize_cut_thumbs(self):
        """Потоковая сверка: нарезка не собирается в список float32.

        100k-кадровый фрагмент в списке — ~1 ГБ; batch обязан читать
        миниатюры нарезки из raw-файла по одной.
        """
        import vc_fragments.views as export_views

        cut1 = self._export(10, 19)
        cut2 = self._export(100, 109)
        with mock.patch.object(
                export_views, "_decode_thumbs",
                side_effect=AssertionError("cut thumbs materialized")):
            export_views._verify_export_cuts(
                self.path, [(10, 19, cut1), (100, 109, cut2)])

    def test_cancel_during_cut_decode_cleans_partials(self):
        """Отмена на декоде нарезки: ExportCancelled и нет temp-файлов."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import ExportCancelled

        cut = self._export(10, 19)
        temp_paths = []
        real_mkstemp = tempfile.mkstemp
        real_decode = export_views._decode_to_raw
        calls = []

        def mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            temp_paths.append(path)
            return fd, path

        def decode(path, frames, raw, cancelled=None, what="эталонов"):
            calls.append(what)
            if what == "нарезки":
                raise ExportCancelled("cut decode cancelled")
            return real_decode(path, frames, raw, cancelled=None)

        with mock.patch.object(
                export_views.tempfile, "mkstemp", side_effect=mkstemp), \
                mock.patch.object(
                    export_views, "_decode_to_raw", side_effect=decode):
            with self.assertRaises(ExportCancelled):
                export_views._verify_export_cuts(
                    self.path, [(10, 19, cut)], cancelled=lambda: True)

        self.assertEqual(calls, ["эталонов", "нарезки"])
        self.assertTrue(temp_paths)
        self.assertFalse(any(os.path.exists(path) for path in temp_paths))

    def test_source_pass_timeout_is_reported(self):
        """Зависший декодер ограничен таймаутом (не ждёт вечно)."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut = self._export(10, 19)

        class HangingProc:
            returncode = None

            def poll(self):
                return None

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        with mock.patch.object(export_views, "_decode_timeout", return_value=0), \
                mock.patch.object(
                    export_views.subprocess, "Popen",
                    return_value=HangingProc()):
            with self.assertRaises(FFmpegError) as ctx:
                export_views._verify_export_cuts(
                    self.path, [(10, 19, cut)])
        self.assertIn("Таймаут", str(ctx.exception))

    def test_source_pass_spawn_failure_is_reported(self):
        """ffmpeg не запускается — FFmpegError с путём исходника."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cut = self._export(10, 19)
        with mock.patch.object(
                export_views.subprocess, "Popen",
                side_effect=OSError("no ffmpeg")):
            with self.assertRaises(FFmpegError) as ctx:
                export_views._verify_export_cuts(
                    self.path, [(10, 19, cut)])
        self.assertIn(self.path, str(ctx.exception))

    def test_empty_batch_is_noop(self):
        """Пустой набор (экспортёр ничего не вернул) — не ошибка."""
        import vc_fragments.views as export_views

        export_views._verify_export_cuts(self.path, [])

