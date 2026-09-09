"""Провайдер кадров видео на сервере.

Отдаёт JPEG-кадр по индексу из файла на диске (original или visualization).
Используется браузером для точной покадровой навигации вперёд/назад.

Движок — PyAV (FFmpeg): seek по точному PTS кадра, что корректнее, чем
``cap.set(CAP_PROP_POS_FRAMES)`` у OpenCV на H.264 с B-кадрами и VFR
(OpenCV там стабильно сдвигался на i+1/i-1 и не читал AV1).
OpenCV остаётся только для JPEG-кодирования и будущих оверлеев.

Точный ``idx -> PTS`` строится из демукс-пакетов (без декодирования),
что для 148k-кадрового файла занимает <1 c; кэшируется по (path, kind).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import List, Optional, Tuple

import av
import cv2
import numpy as np


class FrameCache:
    """Простой потокобезопасный LRU-кэш кадров (индекс -> JPEG-байты)."""

    def __init__(self, capacity: int = 64) -> None:
        self.capacity = capacity
        self._data: "OrderedDict[int, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: int) -> Optional[bytes]:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def put(self, key: int, value: bytes) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)


# Раздельный кэш для каждого (par path) -> (video kind) -> FrameCache.
_cache_by_source: "dict[tuple[str, str], FrameCache]" = {}
_cache_lock = threading.Lock()


def _get_cache(path: str, kind: str) -> FrameCache:
    key = (path, kind)
    with _cache_lock:
        if key not in _cache_by_source:
            _cache_by_source[key] = FrameCache()
        return _cache_by_source[key]


class _AvHandle:
    """Разделяемое открытие PyAV-контейнера + мьютекс на доступ к кадру.

    Один контейнер на (path, kind) для всего процесса; доступ к чтению
    регулируется блокировкой, чтобы не было гонок между загрузкой пары
    и отдачей кадра. ``pts_map`` строится лениво из демукс-пакетов.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:
            self.cont = av.open(path)
        except Exception as e:
            raise ValueError(f"Ошибка открытия видеофайла: {path} ({e})")

        self.stream = self.cont.streams.video[0]
        self.tb = self.stream.time_base
        self.width = self.stream.codec_context.width
        self.height = self.stream.codec_context.height
        self.total_frames = int(self.stream.frames or 0)
        rate = self.stream.average_rate
        self.fps = float(rate) if rate else 0.0
        self._pts_map: Optional[List[float]] = None
        # Обёртки (напр. VP9 webm) часто не отдают stream.frames — реальный
        # счётчик известен только из полного демукса, форсируем его сразу.
        if not self.total_frames:
            self.pts_map()

    def pts_map(self) -> List[float]:
        """Точный список PTS (сек) по индексу кадра, в порядке отображения."""
        with self.lock:
            if self._pts_map is not None:
                return self._pts_map
            pts = []
            for pkt in self.cont.demux(self.stream):
                if pkt.pts is not None:
                    pts.append(pkt.pts * float(pkt.time_base or self.tb))
            pts.sort()
            self._pts_map = pts
            if not self.total_frames:
                self.total_frames = len(pts)
            return pts

    def read_frame(self, index: int) -> Optional[np.ndarray]:
        """Читает кадр по индексу (0-based) как BGR-массив."""
        if not 0 <= index < len(self.pts_map()):
            return None

        with self.lock:
            target_s = self._pts_map[index]  # type: ignore[index]

            # PYAV: seek к предыдущему ключевому кадру; backward=True — до или на.
            offset = int(target_s / float(self.tb)) - 1 if target_s > 0 else 0
            self.cont.seek(offset, stream=self.stream, backward=True, any_frame=False)

            for frame in self.cont.decode(self.stream):
                f_s = (frame.pts or 0) * float(frame.time_base or self.tb)
                if f_s >= target_s:
                    return frame.to_ndarray(format="bgr24")

        return None

    def close(self) -> None:
        with self.lock:
            if self.cont is not None:
                self.cont.close()
                self.cont = None  # type: ignore[assignment]


_caps: "dict[str, _AvHandle]" = {}
_caps_lock = threading.Lock()


def _get_cap(path: str) -> _AvHandle:
    with _caps_lock:
        if path not in _caps:
            _caps[path] = _AvHandle(path)
        return _caps[path]


def _frame_to_jpeg(frame: np.ndarray, quality: int = 92) -> bytes:
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("Не удалось закодировать кадр в JPEG")
    return buf.tobytes()


def get_frame_jpeg(
    path: str,
    index: int,
    *,
    quality: int = 92,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Возвращает (jpeg_bytes, mime) для кадра по индексу или (None, None)."""
    cache = _get_cache(path, "frames")
    cached = cache.get(index)
    if cached is not None:
        return cached, "image/jpeg"

    cap = _get_cap(path)
    frame = cap.read_frame(index)
    if frame is None:
        return None, None
    jpeg = _frame_to_jpeg(frame, quality)
    cache.put(index, jpeg)
    return jpeg, "image/jpeg"


def get_metadata(path: str) -> dict:
    cap = _get_cap(path)
    return {
        "width": cap.width,
        "height": cap.height,
        "total_frames": cap.total_frames,
        "fps": cap.fps,
    }


def close_source(path: str) -> None:
    with _caps_lock:
        cap = _caps.pop(path, None)
        if cap is not None:
            cap.close()