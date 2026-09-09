"""Порты (абстракции) ядра VideoCutter.

Ядро оперирует только этими интерфейсами и не знает, откуда берутся кадры и
куда сохраняются фрагменты. Конкретные реализации (адаптеры) подключаются
снаружи: для локального режима — чтение из файла/сохранение в .txt, для
web-режима — чтение из чанков/сохранение в БД или реестр VideoDataOps.

Эта граница и позволяет VideoCutter жить автономно ИЛИ встраиваться в VDO.
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from .model import Fragment, FragmentList


@runtime_checkable
class IVideoSource(Protocol):
    """Источник кадров видео.

    Даёт произвольный доступ к кадрам по индексу. Реализация может читать
    локальный файл через OpenCV либо доставать кадр из облачного чанка.
    """

    total_frames: int
    width: int
    height: int

    def read(self, index: int) -> Optional[object]:
        """Возвращает кадр по индексу (0-based) или None, если он недоступен.

        Формат кадра определяется реализацией (обычно numpy-массив BGR).
        """
        ...

    def close(self) -> None:
        """Освобождает ресурсы источника."""
        ...


@runtime_checkable
class IFragmentStore(Protocol):
    """Хранилище списка фрагментов.

    Реализация может вести файл .txt, таблицу в БД или запись в реестре данных.
    """

    def load_fragments(self) -> FragmentList:
        """Возвращает сохранённый список фрагментов."""
        ...

    def save_fragments(self, fragments: Sequence[Fragment]) -> None:
        """Сохраняет список фрагментов."""
        ...
