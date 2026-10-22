# -*- coding: utf-8 -*-
"""Приоритет процесса: тяжёлое не должно мешать системе.

Вместо резерва ядер сервис понижает свой nice — системные задачи (UI,
СУБД, сеть, файловая система) получают процессор первыми, а освободившиеся
ядра достаются декодированию. Потоки Linux наследуют nice создавшего
потока, поэтому одной установки в главном потоке хватает всему процессу
(gunicorn, пулы декода и префетча, экспорт).

Значение — env ``VC_NICE`` (по умолчанию 10). Экспорт и без того самый
низкоприоритетный (``VC_EXPORT_NICE`` = 19) — он и дальше последний.
"""

from __future__ import annotations

import os

#: Приоритет по умолчанию: ниже обычного (0), но не «фон».
DEFAULT_NICE = 10


def nice_from_env() -> int:
    """Приоритет из env ``VC_NICE`` (0..19); битое значение — дефолт."""
    raw = os.environ.get("VC_NICE")
    if raw is None or raw == "":
        return DEFAULT_NICE
    try:
        return max(0, min(19, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_NICE


def current_nice() -> int:
    """Текущий nice процесса (0 — обычный приоритет)."""
    try:
        return os.getpriority(os.PRIO_PROCESS, 0)
    except (OSError, AttributeError):
        return 0


def apply_low_priority(nice: int | None = None) -> int:
    """Понизить приоритет процесса; вернуть фактический nice.

    Понизить приоритет (nice вверх) можно всегда; прав на обратное может
    не быть — тогда остаётся текущее значение. Ошибка не ломает старт.
    """
    if nice is None:
        value = nice_from_env()
    else:
        try:
            value = max(0, min(19, int(nice)))
        except (TypeError, ValueError):
            value = DEFAULT_NICE
    try:
        os.setpriority(os.PRIO_PROCESS, 0, value)
    except (OSError, ValueError, AttributeError):
        pass
    return current_nice()
