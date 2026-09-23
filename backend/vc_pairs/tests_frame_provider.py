# -*- coding: utf-8 -*-
"""Тесты GOP-движка кадров (frame_provider).

Прогон:  cd backend && python manage.py test vc_pairs

Генерирует небольшой клип с несколькими ключевыми кадрами (нужен ffmpeg)
и проверяет индекс, кэш групп, префетч и побитовую корректность кадров.
"""

import array
import hashlib
import json
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


def _wait_cache(prov, key, timeout=15.0) -> bool:
    """Ждать, пока GOP (ключ кэша) попадёт в кэш (для префетча/завершения декода)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with prov._ilock:
            if key in prov._cache:
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

    def test_make_bounds_visible_space(self):
        """Границы GOP строятся в индексах видимых кадров."""
        vis = array.array("q", [100_000, 200_000, 300_000, 400_000])
        kf = array.array("q", [100_000, 300_000])
        self.assertEqual(fp._make_bounds(vis, kf), [0, 2, 4])

    def test_make_bounds_missing_keyframe_points_to_next(self):
        """Битый ключевой пакет: граница — первый уцелевший кадр группы."""
        vis = array.array("q", [200_000, 300_000, 400_000])
        kf = array.array("q", [100_000, 300_000])  # 100_000 в vis отсутствует
        self.assertEqual(fp._make_bounds(vis, kf), [0, 1, 3])

    def test_make_bounds_empty(self):
        self.assertEqual(fp._make_bounds(array.array("q"), array.array("q")), [0])

    def test_scan_packets_broken_raises(self):
        with self.assertRaises(ValueError):
            fp._scan_packets("/nonexistent/clip.mp4")


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

    def test_index_scan_parallel_matches_single(self):
        """Сегментный скан побитово равен однопроходному."""
        tb, fps, w, h, total, kf = fp._scan_packets(self.path)
        self.assertGreater(len(kf), 1, "в клипе нет нескольких GOP")
        v1 = fp._scan_visible(self.path, float(tb), kf, 1)
        v4 = fp._scan_visible(self.path, float(tb), kf, 4)
        self.assertEqual(v1, v4)
        self.assertEqual(len(v1), total)
        idx = _prop(self.path)._ensure_index()
        self.assertEqual(list(idx.visible_pts), v1)
        self.assertEqual(idx.bounds, fp._make_bounds(v1, kf))

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
        key0 = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        # Несколько обращений к разным кадрам gop0.
        fp.get_frame_jpeg(self.path, 2)
        self.assertTrue(_wait_cache(prov, key0), "gop0 не попал в кэш")
        # Вытесненные/недостроенные кадры gop0 читаются из кэша практически мгновенно.
        t0 = time.perf_counter()
        for i in (1, 5, 20, 24):
            self.assertIsNotNone(fp.get_frame_jpeg(self.path, i)[0])
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.2, f"кэш-хиты слишком медленные: {elapsed:.3f}s")

    def test_prefetch_warms_next_gop(self):
        prov = _prop(self.path)
        key1 = prov._key(1, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        fp.get_frame_jpeg(self.path, 1)  # касание gop0 запускает префетч
        self.assertTrue(
            _wait_cache(prov, key1),
            "префетч не прогрел gop1 после обращения к gop0",
        )

    def test_prefetch_warms_prev_gop_walking_backward(self):
        prov = _prop(self.path)
        key3 = prov._key(3, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        key2 = prov._key(2, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        fp.get_frame_jpeg(self.path, 90)  # спрос gop3
        self.assertTrue(_wait_cache(prov, key3), "gop3 не задекодилась")
        # Спуск 90 -> 89 -> 88: третий кадр подтверждает направление -1,
        # префетч должен прогреть gop2 (а не gop4, которой нет).
        fp.get_frame_jpeg(self.path, 89)
        fp.get_frame_jpeg(self.path, 88)
        self.assertTrue(
            _wait_cache(prov, key2),
            "обратный префетч не прогрел gop2 при спуске 90->89->88",
        )

    def test_walk_direction_resets_on_jump(self):
        prov = _prop(self.path)
        self.assertEqual(prov._walk_direction(10), 0)  # первый запрос
        self.assertEqual(prov._walk_direction(11), 0)  # второй — ещё 0
        self.assertEqual(prov._walk_direction(12), 1)  # подтверждён +1
        self.assertEqual(prov._walk_direction(12), 0)  # повтор кадра
        self.assertEqual(prov._walk_direction_peek(), 1)  # повтор держит +1
        self.assertEqual(prov._walk_direction(3), 0)  # прыжок назад — сброс
        self.assertEqual(prov._walk_direction_peek(), 0)
        self.assertEqual(prov._walk_direction(2), -1)  # подтверждён -1
        self.assertEqual(prov._walk_direction_peek(), -1)

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
        # Чистый клип: пакетов столько же, сколько видимых кадров, пропусков нет.
        self.assertEqual(meta["packet_frames"], CLIP_FRAMES)
        self.assertEqual(meta["skipped_frames"], 0)

    def test_visible_pts_matches_total_and_monotonic(self):
        """Адресация кадров идёт по display-порядку видимых PTS без дыр."""
        idx = _prop(self.path)._ensure_index()
        self.assertEqual(len(idx.visible_pts), idx.total)
        self.assertEqual(idx.packet_total, idx.total)
        self.assertEqual(idx.skipped, 0)
        for i in range(1, idx.total):
            self.assertLessEqual(idx.visible_pts[i - 1], idx.visible_pts[i])

    def test_scale_produces_smaller_jpeg(self):
        """Кадр с scale=0.5 должен быть существенно меньше (даунскейл → меньше JPEG)."""
        import cv2
        import numpy as np
        j_full, _ = fp.get_frame_jpeg(self.path, 2)
        j_half, _ = fp.get_frame_jpeg(self.path, 2, scale=0.5)
        self.assertIsNotNone(j_full)
        self.assertIsNotNone(j_half)
        # Даунскейл缩小 размер JPEG, но JPEG-кодировщик
        # может по-разному кодировать; проверяем что размер < 70% от оригинала.
        self.assertLess(len(j_half), len(j_full) * 0.7,
                        f"scale=0.5 JPEG ({len(j_half)}) не намного меньше полного ({len(j_full)})")
        img_full = cv2.imdecode(np.frombuffer(j_full, np.uint8), cv2.IMREAD_COLOR)
        img_half = cv2.imdecode(np.frombuffer(j_half, np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(img_full)
        self.assertIsNotNone(img_half)
        h_full, w_full = img_full.shape[:2]
        h_half, w_half = img_half.shape[:2]
        self.assertLess(h_half, h_full)
        self.assertLess(w_half, w_full)

    def test_quality_affects_jpeg_size(self):
        """JPEG-кадр с quality=30 должен быть существенно меньше, чем с quality=90."""
        j_lo, _ = fp.get_frame_jpeg(self.path, 2, quality=30)
        j_hi, _ = fp.get_frame_jpeg(self.path, 2, quality=90)
        self.assertIsNotNone(j_lo)
        self.assertIsNotNone(j_hi)
        self.assertLess(len(j_lo), len(j_hi),
                        f"q30 JPEG ({len(j_lo)}) не меньше, чем q90 ({len(j_hi)})")

    # ─── JPEG-кэш: ключ (GOP, quality, scale), декод один раз на пару ──────

    def test_switch_quality_scale_uses_separate_cache(self):
        """Смена quality/scale создаёт отдельный кэш, декодируя группу повторно —
        зато фиксированные настройки (типичный просмотр) кэшируются компактно."""
        import unittest.mock as mock

        decode_count = 0
        orig = fp._Provider._decode_gop

        def counting_decode(self_, g, task):
            nonlocal decode_count
            decode_count += 1
            return orig(self_, g, task)

        fp.close_source(self.path)
        prov = _prop(self.path)

        # Первый вызов — декодирует GOP 0 под (78, 1.0).
        with mock.patch.object(fp._Provider, "_decode_gop", counting_decode):
            j1, _ = fp.get_frame_jpeg(self.path, 2, quality=78, scale=1.0)
            self.assertTrue(_wait_cache(prov, prov._key(0, 78, 1.0)))
            c1 = decode_count

        # Тот же (quality, scale) — из кэша, без повторного декода.
        with mock.patch.object(fp._Provider, "_decode_gop", counting_decode):
            j2, _ = fp.get_frame_jpeg(self.path, 2, quality=78, scale=1.0)
        self.assertEqual(decode_count, c1, "повторный декод при том же (q, s)!")
        self.assertIsNotNone(j1)
        self.assertIsNotNone(j2)

        # Другие (quality, scale) — отдельный кэш (декод один раз на новую пару).
        with mock.patch.object(fp._Provider, "_decode_gop", counting_decode):
            j3, _ = fp.get_frame_jpeg(self.path, 2, quality=30, scale=0.5)
        self.assertGreater(decode_count, c1)
        self.assertIsNotNone(j3)

    def test_cache_reuses_existing_gop_for_same_settings(self):
        """При фиксированных настройках повторный запрос не передикодирует."""
        import unittest.mock as mock

        decode_count = 0
        orig = fp._Provider._decode_gop

        def counting_decode(self_, g, task):
            nonlocal decode_count
            decode_count += 1
            return orig(self_, g, task)

        fp.close_source(self.path)
        prov = _prop(self.path)

        with mock.patch.object(fp._Provider, "_decode_gop", counting_decode):
            fp.get_frame_jpeg(self.path, 2, quality=78, scale=1.0)
            self.assertTrue(_wait_cache(prov, prov._key(0, 78, 1.0)))
            c1 = decode_count

        # Несколько обращений в ту же группу — без новых декодов.
        with mock.patch.object(fp._Provider, "_decode_gop", counting_decode):
            for i in (0, 5, 10, 20, 24):
                self.assertIsNotNone(
                    fp.get_frame_jpeg(self.path, i, quality=78, scale=1.0)[0])

        self.assertEqual(decode_count, c1,
                         "повторный декод при обращении в ту же группу")


class CacheCapsTest(SimpleTestCase):
    """Runtime-лимиты GOP-кэша: чтение, смена, валидация, вытеснение."""

    def setUp(self):
        self._saved = fp.cache_caps()
        self.addCleanup(fp.set_cache_caps, **self._saved)

    def test_caps_roundtrip(self):
        caps = fp.set_cache_caps(gops=4, mb=64)
        self.assertEqual(caps, {"gops": 4, "mb": 64})
        self.assertEqual(fp.cache_caps(), {"gops": 4, "mb": 64})
        usage = fp.cache_usage()
        self.assertEqual(usage["gops_cap"], 4)
        self.assertEqual(usage["mb_cap"], 64)

    def test_caps_partial_update(self):
        fp.set_cache_caps(gops=7)
        self.assertEqual(fp.cache_caps()["gops"], 7)

    def test_caps_validation(self):
        for kwargs in ({"gops": 0}, {"gops": 99999},
                       {"mb": 8}, {"mb": 99999}, {}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                fp.set_cache_caps(**kwargs)

    def test_shrink_evicts_cache(self):
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден")
        tmpdir = tempfile.TemporaryDirectory(prefix="vccaps_")
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "clip.mp4")
        _make_clip(path)
        self.addCleanup(fp.close_source, path)
        fp.set_cache_caps(gops=16, mb=512)
        prov = _prop(path)
        for i in (1, 30, 60, 90):  # четыре разные GOP
            fp.get_frame_jpeg(path, i)
        keys = [prov._key(g, fp.JPEG_QUALITY, fp.FRAME_SCALE)
                for g in range(4)]
        for k in keys:
            self.assertTrue(_wait_cache(prov, k), f"{k} не попала в кэш")
        fp.set_cache_caps(gops=1)
        with prov._ilock:
            n = len(prov._cache)
        self.assertLessEqual(n, 1, f"сжатие до gops=1 не вытеснило: {n}")

    def test_global_lru_evicts_idle_provider(self):
        """Лимиты машинные: активный провайдер вытесняет idle-соседа."""
        import unittest.mock as mock
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден")
        tmpdir = tempfile.TemporaryDirectory(prefix="vclru_")
        self.addCleanup(tmpdir.cleanup)
        paths = [os.path.join(tmpdir.name, f"c{i}.mp4") for i in (1, 2)]
        for p in paths:
            _make_clip(p)
            self.addCleanup(fp.close_source, p)
        fp.set_cache_caps(gops=2, mb=512)
        provs = [_prop(p) for p in paths]
        keys = [[pr._key(g, fp.JPEG_QUALITY, fp.FRAME_SCALE)
                 for g in range(2)] for pr in provs]
        # Только demand-декоды: веер и цепочки глушим, иначе поздние
        # спекулятивные посадки смажут порядок вытеснения (флейк).
        with mock.patch.object(fp, "PREFETCH_AHEAD", 0), \
                mock.patch.object(fp, "_any_waiters", return_value=True):
            for p in paths:
                for i in (1, 30):  # две GOP провайдера
                    fp.get_frame_jpeg(p, i)
            for pr, ks in zip(provs, keys):
                for k in ks:
                    self.assertTrue(_wait_cache(pr, k),
                                    f"{k} не попала в кэш")
        # Всего 4 GOP при лимите 2: старейшие (первого провайдера) вытеснены,
        # свежие (второго) целы; суммарно в лимите.
        total = sum(len(pr._cache) for pr in provs)
        self.assertLessEqual(total, 2, f"глобальный лимит не держится: {total}")
        with provs[1]._ilock:
            for k in keys[1]:
                self.assertIn(k, provs[1]._cache,
                              "свежие записи активного вытеснены")
        with provs[0]._ilock:
            self.assertEqual(len(provs[0]._cache), 0,
                             "idle-провайдер держит чужое место")

    def test_prefetch_pool_separate(self):
        # Спекуляция — свой пул: иначе пачка префетча подпирает demand-декод
        # в FIFO общего пула (секунды заморозки на холодных границах).
        self.assertIsNot(fp._PREFETCH_POOL, fp._DECODE_POOL)

    def test_fanout_depth_rule(self):
        # Ход вперёд — тишина (вход в GOP с начала отдаёт demand-путь),
        # ход назад — глубокий веер (хвостовой затор), посадка — веер
        # на сторону (вызывающий греет обе).
        self.assertEqual(fp._Provider._fanout_depth(1), 0)
        self.assertEqual(fp._Provider._fanout_depth(0), fp.PREFETCH_AHEAD)
        self.assertEqual(
            fp._Provider._fanout_depth(-1), 2 * fp.PREFETCH_AHEAD)

    def test_landing_warms_both_sides(self):
        """Посадка (направления нет) греет веер вперёд и назад."""
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден")
        tmpdir = tempfile.TemporaryDirectory(prefix="vcland_")
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "clip.mp4")
        _make_clip(path)
        self.addCleanup(fp.close_source, path)
        prov = _prop(path)
        fp.get_frame_jpeg(path, 50)  # первое касание: gop2, direction 0
        key1 = prov._key(1, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        key3 = prov._key(3, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        self.assertTrue(_wait_cache(prov, key1), "назад от посадки не прогрето")
        self.assertTrue(_wait_cache(prov, key3), "вперёд от посадки не прогрето")

    def test_forward_walk_skips_prefetch(self):
        """Подтверждённый ход вперёд не разбрасывает спекулятивные декоды."""
        import unittest.mock as mock
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg не найден")
        tmpdir = tempfile.TemporaryDirectory(prefix="vcwalk_")
        self.addCleanup(tmpdir.cleanup)
        path = os.path.join(tmpdir.name, "clip.mp4")
        _make_clip(path)
        self.addCleanup(fp.close_source, path)
        prov = _prop(path)
        fp.get_frame_jpeg(path, 1)
        key0 = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        self.assertTrue(_wait_cache(prov, key0))

        def quiesce():
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                with prov._ilock:
                    if not prov._tasks:
                        return True
                time.sleep(0.05)
            return False

        self.assertTrue(quiesce(), "фоновые задачи не завершились")
        # Даём спросу протухнуть (PREFETCH_IDLE_S): иначе долетающие
        # завершения порождают цепочки уже под моком — флейк подсчёта.
        # После этого фона нет (цепочки без свежего спроса запрещены),
        # и окно замера детерминировано.
        time.sleep(fp.PREFETCH_IDLE_S + 0.5)
        self.assertTrue(quiesce(), "фон не успокоился")
        # Два шага вперёд подтверждают ход, затем ход по прогретой группе:
        # пул префетча должен молчать.
        with mock.patch.object(fp._PREFETCH_POOL, "submit") as sub:
            fp.get_frame_jpeg(path, 2)
            fp.get_frame_jpeg(path, 3)  # второй шаг подтверждает ход
            sub.reset_mock()
            for i in (4, 5, 6):
                fp.get_frame_jpeg(path, i)
            time.sleep(0.3)
            self.assertEqual(sub.call_count, 0,
                             "ход вперёд должен молчать в пуле префетча")

    def test_auto_tune_grid(self):
        # (ncpu, ram_mb) -> (decode, prefetch, cache_mb, cache_gops).
        cases = [
            ((16, 15197), (14, 4, 1266, 39)),
            ((8, 8192), (6, 2, 682, 21)),
            ((4, 4096), (2, 1, 341, 10)),
            ((2, 2048), (2, 1, 256, 8)),
            ((64, 131072), (62, 20, 4096, 128)),
        ]
        for (ncpu, ram), (d, p, mb, gops) in cases:
            with self.subTest(ncpu=ncpu, ram_mb=ram):
                t = fp._auto_tune(ncpu, ram)
                self.assertEqual(
                    (t["decode_cores"], t["prefetch_workers"],
                     t["cache_mb"], t["cache_gops"]), (d, p, mb, gops))

    def test_env_or_auto_override(self):
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"VC_GOP_CACHE_MB": "777"}):
            self.assertEqual(fp._env_or_auto("VC_GOP_CACHE_MB", 512), 777)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VC_GOP_CACHE_MB", None)
            self.assertEqual(fp._env_or_auto("VC_GOP_CACHE_MB", 512), 512)

    def test_host_resources_sane(self):
        ncpu, ram = fp._host_resources()
        self.assertGreaterEqual(ncpu, 1)
        self.assertGreaterEqual(ram, 256)

    def test_cache_endpoint(self):
        resp = self.client.get("/api/v1/cache")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("caps", body)
        self.assertIn("usage", body)
        self.assertIn("tuning", body)
        self.assertIn("ncpu", body["tuning"])
        resp = self.client.post(
            "/api/v1/cache", data=json.dumps({"gops": 9}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["caps"]["gops"], 9)
        resp = self.client.post(
            "/api/v1/cache", data=json.dumps({"gops": 0}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)


class SpeculationGenTest(SimpleTestCase):
    """Поколение спекуляции: прыжок сбрасывает queued-префетч прошлого места."""

    def test_stale_speculative_dropped(self):
        import unittest.mock as mock
        prov = fp._Provider("/nonexistent/clip.mp4")
        task = fp._GopTask(fp.JPEG_QUALITY, fp.FRAME_SCALE)
        task.speculative = True
        task.gen = 0
        prov._gen = 5
        key = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        prov._tasks[key] = task
        with mock.patch.object(fp._Provider, "_decode_gop") as dec, \
                mock.patch.object(fp._Provider, "_prefetch"):
            prov._run_pooled(key, task)
        dec.assert_not_called()
        self.assertTrue(task.done)
        with prov._ilock:
            self.assertNotIn(key, prov._tasks)

    def test_demand_adopts_speculative(self):
        # Спрос на GOP с живой спекулятивной задачей усыновляет её:
        # gen-сброс ждуна не касается.
        prov = fp._Provider("/nonexistent/clip.mp4")
        task = fp._GopTask(fp.JPEG_QUALITY, fp.FRAME_SCALE)
        task.speculative = True
        task.gen = 3
        key = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        prov._tasks[key] = task
        prov._gen = 7
        got = prov._task_for(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        self.assertIs(got, task)
        self.assertFalse(task.speculative)

    def test_stale_drop_respects_waiters(self):
        # Протухшую задачу, которую ждут, не сбрасываем — декодируем.
        import unittest.mock as mock
        prov = fp._Provider("/nonexistent/clip.mp4")
        task = fp._GopTask(fp.JPEG_QUALITY, fp.FRAME_SCALE)
        task.speculative = True
        task.gen = 0
        with task._lock:
            task.waiters = 1
        prov._gen = 5
        key = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        prov._tasks[key] = task
        with mock.patch.object(fp._Provider, "_decode_gop") as dec, \
                mock.patch.object(fp._Provider, "_prefetch"):
            prov._run_pooled(key, task)
        dec.assert_called_once()
        with task._lock:
            task.waiters = 0

    def test_adopt_done_task_creates_fresh(self):
        # Усыновление done-задачи: создаём свежую demand, а не ждём пустоту.
        import unittest.mock as mock
        prov = fp._Provider("/nonexistent/clip.mp4")
        task = fp._GopTask(fp.JPEG_QUALITY, fp.FRAME_SCALE)
        task.speculative = True
        task.gen = 3
        task.mark_done()
        key = prov._key(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        prov._tasks[key] = task
        prov._gen = 7
        with mock.patch.object(fp._DECODE_POOL, "submit"), \
                mock.patch.object(fp._Provider, "_prefetch"):
            got = prov._task_for(0, fp.JPEG_QUALITY, fp.FRAME_SCALE)
        self.assertIsNot(got, task)
        self.assertFalse(got.speculative)
        self.assertFalse(got.done)

    def test_abandoned_falls_back_to_single(self):
        # Брошенная задача: одиночный декод вместо мгновенного 404.
        import types
        import unittest.mock as mock
        prov = fp._Provider("/nonexistent/clip.mp4")
        idx = types.SimpleNamespace(total=100, bounds=[0, 50, 100],
                                    gop_of=lambda i: 0 if i < 50 else 1)
        prov._ensure_index = lambda: idx
        task = fp._GopTask(fp.JPEG_QUALITY, fp.FRAME_SCALE)
        task.abandoned = True
        task.mark_done()
        with mock.patch.object(fp._Provider, "_task_for",
                               return_value=task), \
                mock.patch.object(fp._Provider, "_read_one",
                                  return_value=b"JPEG") as single, \
                mock.patch.object(fp._Provider, "_prefetch"):
            j, info = prov.get_frame(10)
        self.assertEqual(j, b"JPEG")
        self.assertTrue(info["abandoned"])
        single.assert_called_once()

    def test_walk_start_keeps_generation(self):
        # Старт обхода (второй запрос) — не разрыв: веер посадки живёт.
        # Разрыв подтверждённого хода (прыжок/разворот) — двигает поколение.
        prov = fp._Provider("/nonexistent/clip.mp4")
        prov._walk_direction(10)  # первое касание
        g0 = prov._gen
        prov._walk_direction(11)  # старт: last_step было 0 — без bump
        self.assertEqual(prov._gen, g0)
        prov._walk_direction(12)  # подтверждён: без bump
        self.assertEqual(prov._gen, g0)
        prov._walk_direction(5)  # разворот: bump
        self.assertEqual(prov._gen, g0 + 1)
        prov._walk_direction(6)  # смена знака: bump
        self.assertEqual(prov._gen, g0 + 2)

    def test_chained_prefetch_gated_by_fresh_demand(self):
        # Цепочки без свежего спроса запрещены (иначе каскад до конца
        # файла после ухода пользователя); со свежим — идут.
        import types
        import unittest.mock as mock
        prov = fp._Provider("/nonexistent/clip.mp4")
        fp._providers["/nonexistent/clip.mp4"] = prov
        self.addCleanup(fp._providers.pop, "/nonexistent/clip.mp4", None)
        prov._ensure_index = lambda: types.SimpleNamespace(
            bounds=[0, 25, 50, 75, 96])
        prov._last_demand = 0.0  # спрос умер давно
        with mock.patch.object(fp._PREFETCH_POOL, "submit") as sub:
            prov._prefetch(1, fp.JPEG_QUALITY, fp.FRAME_SCALE, chained=True)
            self.assertEqual(
                sub.call_count, 0,
                "цепочки без свежего спроса запрещены")
            prov._note_demand()  # свежий спрос
            prov._prefetch(1, fp.JPEG_QUALITY, fp.FRAME_SCALE, chained=True)
            self.assertGreater(
                sub.call_count, 0,
                "цепочки со свежим спросом идут")