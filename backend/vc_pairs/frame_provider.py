"""Провайдер кадров видео на сервере.

Отдаёт JPEG-кадр по индексу из файла на диске (original или visualization).

Движок — PyAV (FFmpeg). Точный ``idx -> PTS`` строится из демукс-пакетов
(без декодирования) и кэшируется по (path, kind). Среди ключевых кадров
(I-кадров) строится индекс границ GOP, чтобы декодировать только кадры
внутри одной группы.

Почему так: у H.264 не-ключевой кадр доступен только через декодирование
всей цепочки от ближайшего I-кадра. Один „холодный" запрос к середине GOP
требует декода всей группы (у crowd-подобных файлов GOP ~ 250 кадров,
1080p → 1.5–2 c — это и был источник «тормозов» при перемотке). Решение —
декодировать GOP один раз целиком, кэшировать все её кадры и префетчить
соседние группы в фоне:

  * ``_GopCache`` (LRU по GOP, лимиты: число групп и общий объём) — хит =
    чтение из памяти без декода;
  * прогрессивная отдача: кадр уходит клиенту сразу после его энкода,
    остальные кадры группы дозаполняются тем же декодом;
  * фоновый префетч GOP g+1, g+2 при обращении к g;
  * декод групп параллелен по ядрам (пул от ``os.cpu_count()``,
    регулируется env ``VC_DECODE_CORES``; число одновременных декодов
    ограничено памятью через семафор);
  * точный seek к PTS ключевого кадра (без ``-1``: при захвате цели на
    самом I-кадре декодируется 1 кадр).
"""

from __future__ import annotations

import bisect
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import av
import cv2

# --- Конфигурация (зависит от числа ядер/памяти, переопределяется env) ---
_DEFAULT_CORES = os.cpu_count() or 1
# Одновременных декодов GOP. Для 1080p-кадра транзиентно ~6 МБ BGR + накопленный
# JPEG группы, поэтому пул ограничиваем сверху независимо от числа ядер.
DECODE_CORES = int(os.environ.get("VC_DECODE_CORES") or min(8, _DEFAULT_CORES))
PREFETCH_AHEAD = int(os.environ.get("VC_PREFETCH_AHEAD") or 3)
CACHE_MB = int(os.environ.get("VC_GOP_CACHE_MB") or 512)
CACHE_GOPS = int(os.environ.get("VC_GOP_CACHE_GOPS") or 16)
JPEG_QUALITY = int(os.environ.get("VC_JPEG_QUALITY") or 78)
# Масштаб кадров по умолчанию (1.0 = оригинал); фронтенд обычно передаёт свой.
FRAME_SCALE = float(os.environ.get("VC_FRAME_SCALE") or 1.0)

# Допустимые диапазоны per-request параметров (качество JPEG и масштаб).
QUALITY_MIN, QUALITY_MAX = 20, 95
SCALE_MIN, SCALE_MAX = 0.05, 1.0

_DECODE_POOL = ThreadPoolExecutor(max_workers=DECODE_CORES)
# Страховка по памяти: потоков декода не больше, чем DECODE_CORES, т.к. вся
# работа идёт через пул; транзиент на 1080p-группу ~6 МБ BGR + JPEG группы.
_HEAVY = threading.BoundedSemaphore(DECODE_CORES)


class _Index:
    """Индекс кадров: display-порядок PTS и границы GOP по ключевым кадрам."""

    def __init__(self, path: str) -> None:
        try:
            cont = av.open(path)
        except Exception as e:
            raise ValueError(f"Ошибка открытия видеофайла: {path} ({e})")
        stream = cont.streams.video[0]
        self.tb = stream.time_base
        self.width = stream.codec_context.width
        self.height = stream.codec_context.height
        rate = stream.average_rate
        self.fps = float(rate) if rate else 0.0

        pts: List[float] = []
        kf_sec: List[float] = []
        for pkt in cont.demux(stream):
            if pkt.pts is None:
                continue
            tb = float(pkt.time_base or self.tb)
            pts.append(pkt.pts * tb)
            if pkt.is_keyframe:
                kf_sec.append(pkt.pts * tb)
        cont.close()
        pts.sort()
        self.total = len(pts)
        self.pts = pts

        # Индексы отображения ключевых кадров (номер кадра, где стоит I-кадр).
        kf_idx = sorted({
            biz for biz in (bisect.bisect_right(pts, t) - 1 for t in kf_sec)
            if 0 <= biz < self.total
        })
        if not kf_idx or kf_idx[0] != 0:
            kf_idx = [0] + kf_idx
        if kf_idx[-1] != self.total:
            kf_idx.append(self.total)
        self.bounds: List[int] = kf_idx

    def gop_of(self, frame: int) -> Optional[int]:
        """Номер GOP, содержащего кадр, или None (до первого ключевого)."""
        if not 0 <= frame < self.total:
            return None
        g = bisect.bisect_right(self.bounds, frame) - 1
        return g if 0 <= g < len(self.bounds) - 1 else None


class _GopTask:
    """Текущий декод одной GOP: кадры появляются по мере энкода.

    Читатели (в т.ч. из префетч-потока) ждут свой кадр по событиям.
    """

    def __init__(self) -> None:
        self.frames: Dict[int, bytes] = {}
        self._events: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.error: Optional[BaseException] = None
        self.done = False

    def put(self, frame: int, jpeg: bytes) -> None:
        with self._lock:
            self.frames[frame] = jpeg
            ev = self._events.pop(frame, None)
        if ev is not None:
            ev.set()

    def mark_done(self) -> None:
        with self._lock:
            self.done = True

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            self.error = exc
            self.done = True

    def wait(self, frame: int) -> Optional[bytes]:
        """Ждать кадр (или завершение декода без него)."""
        while True:
            with self._lock:
                j = self.frames.get(frame)
                err = self.error
                done = self.done
            if j is not None:
                return j
            if err is not None or done:
                return None
            self._event_for(frame).wait(0.15)

    def _event_for(self, frame: int) -> threading.Event:
        with self._lock:
            ev = self._events.get(frame)
            if ev is None:
                ev = threading.Event()
                self._events[frame] = ev
            return ev


class _Provider:
    """Провайдер одного видеофайла: индекс + кэш GOP + префетч.

    Кадры кэшируются как готовый JPEG по ключу ``(gop, quality, scale)``:
    разные настройки просмотра (ползунки качества/масштаба) — разные кэши.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._index: Optional[_Index] = None
        self._ilock = threading.Lock()
        self._cache: "OrderedDict[tuple, Dict[int, bytes]]" = OrderedDict()
        self._bytes = 0
        self._tasks: Dict[tuple, _GopTask] = {}
        self._scheduled: set[tuple] = set()

    @staticmethod
    def _key(g: int, quality: int, scale: float) -> tuple:
        """Ключ кэша GOP: номер группы + параметры просмотра (масштаб округляем)."""
        return (g, int(quality), round(float(scale), 2))

    # --- индекс ---
    def _ensure_index(self) -> _Index:
        if self._index is None:
            with self._ilock:
                if self._index is None:
                    self._index = _Index(self.path)
        return self._index

    # --- кэш ---
    def _cache_hit(self, key: tuple, index: int) -> Optional[bytes]:
        with self._ilock:
            d = self._cache.get(key)
            if d is None:
                return None
            self._cache.move_to_end(key)
            return d.get(index)

    def _cache_put(self, key: tuple, frames: Dict[int, bytes]) -> None:
        with self._ilock:
            if key in self._cache:
                return
            self._cache[key] = frames
            self._bytes += sum(len(b) for b in frames.values())
        self._evict()

    def _evict(self) -> None:
        while (len(self._cache) > CACHE_GOPS) or (self._bytes > CACHE_MB << 20):
            with self._ilock:
                self._cache.popitem(last=False)

    # --- декод GOP ---
    @staticmethod
    def _encode(frame, quality: int, scale: float) -> Optional[bytes]:
        bgr = frame.to_ndarray(format="bgr24")
        if scale < (1.0 - 1e-6):
            h, w = bgr.shape[:2]
            nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
            if (nh, nw) != (h, w):
                bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    def _decode_gop(self, g: int, quality: int, scale: float, task: _GopTask) -> None:
        idx = self._ensure_index()
        start = idx.bounds[g]
        end = min(idx.bounds[g + 1], idx.total)
        tb = float(idx.tb)
        seek_tick = int(idx.pts[start] / tb + 0.5)
        try:
            with _HEAVY:
                cont = av.open(self.path)
                try:
                    stream = cont.streams.video[0]
                    cont.seek(seek_tick, stream=stream, backward=True, any_frame=False)
                    i = -1
                    for fr in cont.decode(stream):
                        fs = (fr.pts or 0) * float(fr.time_base or tb)
                        if fs < idx.pts[start]:
                            continue  # B-кадры предыдущей GOP в display-порядке
                        i += 1
                        gi = start + i
                        if gi >= end:
                            break
                        jpeg = self._encode(fr, quality, scale)
                        if jpeg is not None:
                            task.put(gi, jpeg)
                finally:
                    cont.close()
            task.mark_done()
        except BaseException as e:  # noqa: BLE001 — завершение задачи для ждунов
            task.fail(e)

    def _run_pooled(self, key: tuple, task: _GopTask) -> None:
        g, q, s = key
        self._decode_gop(g, q, s, task)
        self._finish_task(key, task)

    def _finish_task(self, key: tuple, task: _GopTask) -> None:
        with self._ilock:
            self._tasks.pop(key, None)
            self._scheduled.discard(key)
        if not task.error and task.frames:
            self._cache_put(key, task.frames)
        self._prefetch(key)

    def _prefetch(self, key: tuple) -> None:
        g, q, s = key
        idx = self._ensure_index()
        for d in range(1, PREFETCH_AHEAD + 1):
            ng = g + d
            if ng >= len(idx.bounds) - 1:
                break
            nkey = self._key(ng, q, s)
            with self._ilock:
                if nkey in self._cache or nkey in self._tasks or nkey in self._scheduled:
                    continue
                self._scheduled.add(nkey)
                task = _GopTask()
                self._tasks[nkey] = task
            try:
                _DECODE_POOL.submit(self._run_pooled, nkey, task)
            except Exception:
                # Пул остановлен (завершение приложения) — убираем как несостоявшийся.
                with self._ilock:
                    self._tasks.pop(nkey, None)
                    self._scheduled.discard(nkey)

    # --- чтение кадра ---
    def get_frame(self, index: int, quality: int = JPEG_QUALITY,
                  scale: float = FRAME_SCALE) -> Optional[bytes]:
        idx = self._ensure_index()
        if not 0 <= index < idx.total:
            return None

        g = idx.gop_of(index)
        if g is not None:
            key = self._key(g, quality, scale)
            j = self._cache_hit(key, index)
            if j is not None:
                # Группа в кэше — прогреваем следующие заранее, чтобы перемотка
                # дальше не упиралась в холодную границу.
                self._prefetch(key)
                return j
            task = self._task_for(key)
            if task is not None:
                j = task.wait(index)
                if j is not None:
                    return j
        return self._read_one(index, quality, scale)

    def _task_for(self, key: tuple) -> Optional[_GopTask]:
        """Создать (и запустить в пуле) задачу GOP, если её ещё нет."""
        with self._ilock:
            task = self._tasks.get(key)
            if task is None:
                task = _GopTask()
                self._tasks[key] = task
                created = True
            else:
                created = False
        if created:
            try:
                _DECODE_POOL.submit(self._run_pooled, key, task)
            except Exception:
                # Пул остановлен (завершение приложения) — как несостоявшийся.
                with self._ilock:
                    self._tasks.pop(key, None)
                    self._scheduled.discard(key)
                return None
            # Следующие группы прогреваем сразу — параллельно текущему декоду,
            # тогда переход через границу не застанет холодную группу.
            self._prefetch(key)
        return task

    # --- одиночный seek (до первого ключевого кадра и прочие огрехи) ---
    def _read_one(self, index: int, quality: int, scale: float) -> Optional[bytes]:
        idx = self._ensure_index()
        if not 0 <= index < idx.total:
            return None
        tb = float(idx.tb)
        target_sec = idx.pts[index]
        g = idx.gop_of(index)
        seek_sec = idx.pts[idx.bounds[g]] if g is not None else idx.pts[0]
        seek_tick = int(seek_sec / tb + 0.5)
        try:
            cont = av.open(self.path)
            try:
                stream = cont.streams.video[0]
                cont.seek(seek_tick, stream=stream, backward=True, any_frame=False)
                for fr in cont.decode(stream):
                    fs = (fr.pts or 0) * float(fr.time_base or tb)
                    if fs >= target_sec:
                        jpeg = self._encode(fr, quality, scale)
                        return jpeg
            finally:
                cont.close()
        except (av.FFmpegError, ValueError):
            return None
        return None

    def metadata(self) -> dict:
        idx = self._ensure_index()
        return {
            "width": idx.width,
            "height": idx.height,
            "total_frames": idx.total,
            "fps": idx.fps,
        }


# --- регистр провайдеров ---
_providers: "Dict[str, _Provider]" = {}
_providers_lock = threading.Lock()


def _get_provider(path: str) -> _Provider:
    with _providers_lock:
        p = _providers.get(path)
        if p is None:
            p = _Provider(path)
            _providers[path] = p
        return p


def get_frame_jpeg(path: str, index: int, *, quality: int = JPEG_QUALITY,
                   scale: float = FRAME_SCALE):
    """Возвращает (jpeg_bytes, mime) для кадра по индексу или (None, None).

    ``quality`` — качество JPEG; ``scale`` — масштаб кадра (0.05..1.0):
    даунскейл применяется до энкода, кадр уходит клиенту уже уменьшенным.
    """
    jpeg = _get_provider(path).get_frame(index, quality, scale)
    return (jpeg, "image/jpeg") if jpeg else (None, None)


def get_metadata(path: str) -> dict:
    return _get_provider(path).metadata()


def close_source(path: str) -> None:
    with _providers_lock:
        _providers.pop(path, None)