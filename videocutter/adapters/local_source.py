"""Чтение кадров из локального видеофайла через OpenCV.

Реализация порта ``IVideoSource``. Предоставляет произвольный доступ к кадру
по индексу: при последовательном запросе читается следующий кадр, при запросе
назад (или вперёд с пропуском) файл перечитывается с начала до нужной позиции.
"""

from __future__ import annotations

from typing import Optional

import cv2

from ..core.ports import IVideoSource


class LocalVideoSource(IVideoSource):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.cap = cv2.VideoCapture(filename)
        if not self.cap.isOpened():
            raise ValueError('Ошибка открытия файла "%s"' % filename)

        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self._pos = -1  # индекс последнего прочитанного кадра

    def read(self, index: int) -> Optional[object]:
        """Возвращает кадр по индексу (numpy BGR) или None, если недоступен."""
        if not 0 <= index < self.total_frames:
            return None

        # Последовательное чтение: просто берём следующий кадр.
        if index == self._pos + 1:
            ok, frame = self.cap.read()
            if not ok:
                return None
            self._pos = index
            return frame

        # Назад или вперёд с пропуском: перечитываем с начала (или ближайшей позиции).
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = self.cap.read()
        if not ok:
            return None
        self._pos = index
        return frame

    def close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None
