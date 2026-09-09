"""Отрисовка таймлайн-статусбара под кадром.

Чистая функция над numpy-массивом: зелёный фон (невыбранное), красный —
фрагменты, затемнение уже просмотренных кадров, голубая подсветка выбранного
диапазона. Не зависит от GUI.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import numpy as np


def draw_statusbar(
    fragments: Sequence[Tuple[int, int]],
    total_frames: int,
    width: int,
    height: int,
    position: int = 0,
    select_range: Optional[Tuple[int, int]] = None,
) -> np.ndarray:
    """Возвращает массив (height, width, 3) uint8 — таймлайн фрагментов."""
    sb = np.zeros((height, width, 3), np.uint8)

    def start_axis(frame: int) -> int:
        return int(round((width - 1) * frame / total_frames)) if total_frames else 0

    def end_axis(frame: int) -> int:
        return start_axis(frame) + 1

    # Зелёный фон для всего таймлайна (уже заполнен сверху).
    sb[:, :, 1] = 255

    # Красные участки — фрагменты.
    for start_frame, end_frame in fragments:
        s = start_axis(start_frame)
        e = end_axis(end_frame)
        sb[:, s:e, 2] = 255
        sb[:, s:e, 1] = 0

    # Затемняем уже просмотренные кадры.
    current_shift = int(width * position / total_frames) if total_frames else 0
    sb[:, current_shift:, :] //= 2

    # Голубая подсветка выбираемого диапазона.
    if select_range is not None:
        s = start_axis(select_range[0])
        e = end_axis(select_range[1])
        sb[:, s:e, 0] = 255

    return sb
