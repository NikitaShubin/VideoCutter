"""Последовательный покадровый читатель видео с кольцевым буфером.

Работает поверх порта ``IVideoSource``, поэтому не зависит от того, откуда
берутся кадры (локальный файл или облачные чанки). Буфер позволяет двигаться
вперёд/назад в пределах окна без повторного чтения с диска.

По желанию (размер буфера — параметр) и для выхода за пределы буфера в прошлое
предусмотрен ``seek()``, который перестраивает окно через произвольный доступ
источника (чанки дают случайный доступ; локальные файлы могут перечитываться
с начала).
"""

from __future__ import annotations

from typing import Optional

from .ports import IVideoSource
from .model import CircleInd


class VideoReader:
    def __init__(self, source: IVideoSource, buf_size: int = 512) -> None:
        self.source = source
        self.w = source.width
        self.h = source.height
        self.total_frames = source.total_frames

        if self.total_frames is not None:
            self.buf_size = max(1, min(int(buf_size), int(self.total_frames)))
        else:
            self.buf_size = max(1, int(buf_size))

        # Предвыделяем буфер кадров (кадры — numpy-массивы BGR).
        self.buffer: list = [None] * self.buf_size

        # Заполняем буфер начальным окном кадров.
        self._fill_window(0)

        self.start = CircleInd(self.buf_size)
        self.cur = CircleInd(self.buf_size)
        self.end = CircleInd(self.buf_size, self.buf_size - 1)
        self.frame_ind = len(self.buffer) - 1  # последний кадр в буфере
        self.position = 0

    def _fill_window(self, first_index: int) -> None:
        """Заполняет весь буфер кадрами, начиная с first_index."""
        for i in range(self.buf_size):
            index = first_index + i
            if index >= self.total_frames:
                self.buffer[i] = None
            else:
                self.buffer[i] = self.source.read(index)
        self.start = CircleInd(self.buf_size)
        self.cur = CircleInd(self.buf_size)
        self.end = CircleInd(self.buf_size, self.buf_size - 1)
        self.frame_ind = first_index + self.buf_size - 1
        self.position = first_index

    def current(self):
        frame = self.buffer[self.cur()]
        if frame is None:
            return None
        return frame

    def next(self, get_frame: bool = True):
        # Следующий кадр уже есть в буфере.
        if self.cur != self.end:
            self.cur.inc()
            self.position += 1
            return self.current() if get_frame else None

        # Текущий кадр — последний в видео.
        if self.frame_ind >= self.total_frames - 1:
            return None

        # Берём следующий кадр с диска/источника.
        self.frame_ind += 1
        self.start.inc()
        self.cur.inc()
        self.end.inc()
        new_frame = self.source.read(self.frame_ind)
        self.buffer[self.cur()] = new_frame
        self.position += 1
        return new_frame if get_frame else None

    def prev(self, get_frame: bool = True):
        # Курсор уже на самом раннем кадре буфера или на первом кадре видео.
        if self.cur == self.start or not self.position:
            return None

        self.cur.dec()
        self.position -= 1
        return self.current() if get_frame else None

    def seek(self, position: int) -> object:
        """Перемещает текущий кадр в позицию (0-based) и возвращает его.

        Если позиция за пределами текущего окна буфера, окно перестраивается:
        - при движении вперёд/назад в пределах видео — через произвольный доступ
          источника (источник сам решает, как: чанки — случайно, файлы — перебором);
        - если источник не поддерживает произвольный доступ, адаптер перечитывает
          кадры с начала, что и даёт возможность уходить в прошлое за буфер.
        """
        if not 0 <= position < self.total_frames:
            return None

        # Если позиция внутри текущего окна буфера — просто двигаем курсор.
        first = self.position - int(self.cur)
        if first <= position <= first + self.buf_size - 1:
            delta = position - self.position
            if delta >= 0:
                for _ in range(delta):
                    self.cur.inc()
                    self.position += 1
            else:
                for _ in range(-delta):
                    self.cur.dec()
                    self.position -= 1
            return self.current()

        # Иначе перестраиваем окно с нужного кадра.
        start_first = max(0, position)
        self._fill_window(start_first)
        self.position = start_first
        # Если запрошенная позиция оказалась правее первого кадра окна.
        self.cur = CircleInd(self.buf_size)
        shift = position - start_first
        for _ in range(shift):
            self.cur.inc()
            self.position += 1
        return self.current()

    def close(self) -> None:
        self.source.close()
