# -*- coding: utf-8 -*-
"""Проверка свободного места перед созданием файлов.

Очередь не должна забивать диск под ноль: загрузка архива и сборка
результата сначала спрашивают, хватит ли места с запасом. Запас — env
``VC_DISK_MIN_FREE_MB`` (по умолчанию 512 МБ).
"""

from __future__ import annotations

import os
import shutil


class InsufficientSpace(OSError):
    """Свободного места меньше требуемого (ENOSPC)."""


def min_free_bytes() -> int:
    try:
        mb = int(os.getenv("VC_DISK_MIN_FREE_MB", "512"))
    except ValueError:
        mb = 512
    return max(0, mb) * 1024 * 1024


def free_bytes(path: str) -> int:
    """Свободно на разделе, где лежит ``path`` (или его ближайший родитель)."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return 0


def ensure_free(path: str, need_bytes: int = 0,
                reserve_bytes: int = None) -> None:
    """Бросает ``InsufficientSpace``, если места не хватает.

    ``need_bytes`` — сколько собираемся записать, ``reserve_bytes`` —
    неприкосновенный запас (по умолчанию из env).
    """
    reserve = (min_free_bytes() if reserve_bytes is None
               else max(0, int(reserve_bytes)))
    need = max(0, int(need_bytes))
    free = free_bytes(path)
    if free < need + reserve:
        raise InsufficientSpace(
            f"Нет места на диске: свободно {free // (1024 * 1024)} МБ, "
            f"нужно {need // (1024 * 1024)} МБ + запас "
            f"{reserve // (1024 * 1024)} МБ")
