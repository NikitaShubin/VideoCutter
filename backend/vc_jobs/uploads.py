# -*- coding: utf-8 -*-
"""Загруженные архивы для фоновых импортов.

Файл кладётся в ``VC_STATE_ROOT/uploads/<uuid>.zip`` и живёт там сутки:
операция получает идентификатор, а не многогигабайтное тело. Это также
даёт идемпотентность — повторный запуск с тем же ``upload_id`` не требует
повторной передачи файла.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from typing import Optional

import vc_jobs

_DIR = "uploads"
_DEFAULT_RETENTION_DAYS = 1.0


def root() -> str:
    return os.path.join(vc_jobs.state_root(), _DIR)


def _max_bytes() -> int:
    try:
        mb = int(os.getenv("VC_JOB_UPLOAD_MAX_MB", "8192"))
    except ValueError:
        mb = 8192
    return max(1, mb) * 1024 * 1024


def _retention_s() -> float:
    try:
        days = float(os.getenv("VC_JOB_UPLOAD_RETENTION_DAYS", "1"))
    except ValueError:
        days = _DEFAULT_RETENTION_DAYS
    return max(0.0, days) * 86400


def prune(max_age_s: Optional[float] = None) -> None:
    """Убирает старые загрузки и недописанные временные файлы."""
    directory = root()
    try:
        names = os.listdir(directory)
    except OSError:
        return

    limit = (time.time() - (_retention_s() if max_age_s is None
                             else max_age_s))
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < limit:
                os.remove(path)
        except OSError:
            pass


def save(upload) -> dict:
    """Сохраняет Django UploadedFile; возвращает ``{id, name, size}``."""
    size = int(getattr(upload, "size", 0) or 0)
    if size > _max_bytes():
        raise ValueError(
            f"архив больше {_max_bytes() // (1024 * 1024)} МБ")
    upload_id = uuid.uuid4().hex
    directory = root()
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".part-", dir=directory)
    final = os.path.join(directory, upload_id + ".zip")
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)
        os.replace(temp, final)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    prune()
    return {"id": upload_id, "name": getattr(upload, "name", "") or "archive.zip",
            "size": size}


def resolve(upload_id: str) -> Optional[str]:
    """Путь загруженного архива; None — нет такого id (в т.ч. traversal)."""
    if not isinstance(upload_id, str) or len(upload_id) != 32:
        return None
    try:
        uuid.UUID(hex=upload_id)
    except ValueError:
        return None
    path = os.path.join(root(), upload_id + ".zip")
    return path if os.path.isfile(path) else None


def clear() -> None:
    shutil.rmtree(root(), ignore_errors=True)
