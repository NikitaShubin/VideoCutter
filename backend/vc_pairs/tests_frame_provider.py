# -*- coding: utf-8 -*-
"""Тесты GOP-движка кадров (frame_provider).

Прогон:  cd backend && python manage.py test vc_pairs

Генерирует небольшой клип с несколькими ключевыми кадрами (нужен ffmpeg)
и проверяет индекс, кэш групп, префетч и побитовую корректность кадров.
"""

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from django.test import SimpleTestCase

from vc_pairs import frame_provider as fp
from vc_pairs.frame_provider import _Index

# Конфиг теста: маленький клип, чтобы декод был быстрым.
CLIP_SPEC = "testsrc2=size=96x96:rate=24:duration=4"
CLIP_GOP = 25
CLIP_FRAMES = 96


def _make_clip(path: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", CLIP_SPEC,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-g", str(CLIP_GOP), "-pix_fmt", "yuv420p", path,
        ],
        check=True,
        capture_output=True,
    )


def _prop(path: str) -> fp._Provider:
    return fp._get_provider(path)


def _wait_cache(prov, g, timeout=15.0) -> bool:
    """Ждать, пока GOP попадёт в кэш (для префетча/завершения декода)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with prov._ilock:
            if g in prov._cache:
                return True
        time.sleep(0.02)
    return False


class GopIndexTest(SimpleTestCase):
    """Чистые юнит-тесты геометрии GOP без медиа."""

    def test_gop_of_bounds(self):
        idx = object.__new__(_Index)
        idx.total = 100
        idx.bounds = [0, 25, 50, 75, 100]
        for n in range(100):
            want = n // 25
            self.assertEqual(idx.gop_of(n), want)
        self.assertIsNone(idx.gop_of(-1))
        self.assertIsNone(idx.gop_of(100))
        self.assertIsNone(idx.gop_of(200))

    def test_gop_of_before_first_keyframe(self):
        idx = object.__new__(_Index)
        idx.total = 100
        idx.bounds = [7, 40, 100]
        self.assertIsNone(idx.gop_of(6))
        self.assertEqual(idx.gop_of(7), 0)
        self.assertEqual(idx.gop_of(39), 0)
        self.assertEqual(idx.gop_of(40), 1)
        self.assertEqual(idx.gop_of(99), 1)


class FrameProviderTest(SimpleTestCase):
    """Интеграционные тесты на настоящем клипе."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден — тесты GOP-движка скипаются")
        super().setUpClass()
        cls.tmpdir = tempfile.TemporaryDirectory(prefix="vcframes_")
        cls.path = os.path.join(cls.tmpdir.name, "clip.mp4")
        _make_clip(cls.path)
        fp.close_source(cls.path)

    @classmethod
    def tearDownClass(cls):
        fp.close_source(cls.path)
        cls.tmpdir.cleanup()
        super().tearDownClass()

    def tearDown(self):
        fp.close_source(self.path)

    def test_index_multi_gop(self):
        idx = _prop(self.path)._ensure_index()
        self.assertEqual(idx.total, CLIP_FRAMES)
        self.assertGreaterEqual(len(idx.bounds), 4)  # 0,25,50,75,96
        self.assertEqual(idx.bounds[0], 0)
        self.assertEqual(idx.bounds[-1], CLIP_FRAMES)
        # Больше одного ключевого кадра — иначе тест бессмысленен.
        self.assertGreater(len([b for b in idx.bounds if b < CLIP_FRAMES]), 2)

    def test_serve_all_frames(self):
        import cv2
        import numpy as np
        idx = _prop(self.path)._ensure_index()
        for i in range(CLIP_FRAMES):
            j, mime = fp.get_frame_jpeg(self.path, i)
            self.assertIsNotNone(j)
            self.assertEqual(mime, "image/jpeg")
            img = cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR)
            self.assertIsNotNone(img, f"битый JPEG кадра {i}")

    def test_cache_hit_is_fast(self):
        prov = _prop(self.path)
        # Несколько обращений к разным кадрам gop0.
        fp.get_frame_jpeg(self.path, 2)
        self.assertTrue(_wait_cache(prov, 0), "gop0 не попал в кэш")
        # Вытесненные/недостроенные кадры gop0 читаются из кэша практически мгновенно.
        t0 = time.perf_counter()
        for i in (1, 5, 20, 24):
            self.assertIsNotNone(fp.get_frame_jpeg(self.path, i)[0])
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.2, f"кэш-хиты слишком медленные: {elapsed:.3f}s")

    def test_prefetch_warms_next_gop(self):
        prov = _prop(self.path)
        fp.get_frame_jpeg(self.path, 1)  # касание gop0 запускает префетч
        self.assertTrue(
            _wait_cache(prov, 1),
            "префетч не прогрел gop1 после обращения к gop0",
        )

    def test_frames_match_reference(self):
        """Побитовое совпадение GOP-движка с последовательным декодом."""
        import cv2
        import av
        ref = {}
        cont = av.open(self.path)
        stream = cont.streams.video[0]
        for i, fr in enumerate(cont.decode(stream)):
            ok, buf = cv2.imencode(
                ".jpg", fr.to_ndarray(format="bgr24"),
                [int(cv2.IMWRITE_JPEG_QUALITY), fp.JPEG_QUALITY])
            self.assertTrue(ok)
            ref[i] = hashlib.sha256(buf.tobytes()).hexdigest()
        cont.close()

        # Точки по всем границам GOP + края.
        idx = _prop(self.path)._ensure_index()
        sample = set(range(0, CLIP_FRAMES, 7))
        for b in idx.bounds[:-1]:
            sample.update((max(0, b - 1), b, min(CLIP_FRAMES - 1, b + 1)))
        for i in sample:
            j, _ = fp.get_frame_jpeg(self.path, i)
            want = hashlib.sha256(j).hexdigest()
            self.assertEqual(want, ref[i], f"кадр #{i} расходится с эталоном")

    def test_metadata(self):
        meta = fp.get_metadata(self.path)
        self.assertEqual(meta["total_frames"], CLIP_FRAMES)
        self.assertEqual(meta["width"], 96)
        self.assertEqual(meta["height"], 96)
        self.assertGreater(meta["fps"], 10)