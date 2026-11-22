# -*- coding: utf-8 -*-
"""Загруженные архивы для фоновых импортов.

Файл кладётся в ``VC_STATE_ROOT/uploads/<uuid>.zip`` и живёт там сутки:
операция получает идентификатор, а не многогигабайтное тело. Это также
даёт идемпотентность — повторный запуск с тем же ``upload_id`` не требует
повторной передачи файла.

Рядом лежит ``<uuid>.json`` с владельцем. Чужой upload нельзя использовать
для импорта (кроме администратора), а пока на файл ссылается активная
операция, TTL его не удаляет.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from typing import Iterable, Optional

import vc_jobs

from . import disk

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


def _path(upload_id: str) -> str:
    return os.path.join(root(), upload_id + ".zip")


def _meta_path(upload_id: str) -> str:
    return os.path.join(root(), upload_id + ".json")


def _valid_id(upload_id) -> bool:
    if not isinstance(upload_id, str) or len(upload_id) != 32:
        return False
    try:
        uuid.UUID(hex=upload_id)
    except ValueError:
        return False
    return True


def _write_meta(upload_id: str, owner: str, name: str, size: int) -> None:
    directory = root()
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".meta-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"id": upload_id, "owner": owner or "local",
                       "name": name, "size": int(size)}, fh,
                      ensure_ascii=False)
        os.replace(temp, _meta_path(upload_id))
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _read_meta(upload_id: str) -> Optional[dict]:
    try:
        with open(_meta_path(upload_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def owner(upload_id: str) -> Optional[str]:
    """Владелец загрузки; None — файла нет.

    Старые загрузки без sidecar считаются локальными: в автономном режиме
    это единственный владелец, а в VDO ими распорядится администратор.
    """
    if not _valid_id(upload_id) or not os.path.isfile(_path(upload_id)):
        return None
    meta = _read_meta(upload_id)
    if meta is None:
        return "local"
    return str(meta.get("owner") or "local")


def _pinned_by_active_jobs() -> set:
    pinned = set()
    try:
        jobs = vc_jobs.active_jobs()
    except Exception:  # noqa: BLE001 — уборка не должна мешать загрузке
        return pinned
    for job in jobs:
        if job.target.get("kind") == "upload":
            pinned.add(job.target.get("id"))
        upload_id = job.params.get("upload_id")
        if isinstance(upload_id, str) and upload_id:
            pinned.add(upload_id)
    return pinned


def prune(pinned: Optional[Iterable[str]] = None,
          max_age_s: Optional[float] = None) -> None:
    """Убирает старые загрузки; ``pinned`` не трогает даже просроченные."""
    directory = root()
    try:
        names = os.listdir(directory)
    except OSError:
        return

    if pinned is None:
        pinned = _pinned_by_active_jobs()
    pinned_ids = {str(item) for item in pinned if item}
    limit = time.time() - (_retention_s() if max_age_s is None
                           else max_age_s)
    for name in names:
        upload_id = name.split(".", 1)[0]
        if upload_id in pinned_ids:
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < limit:
                os.remove(path)
        except OSError:
            pass


def save(upload, owner: str = "local") -> dict:
    """Сохраняет Django UploadedFile; возвращает ``{id, name, size, owner}``."""
    size = int(getattr(upload, "size", 0) or 0)
    if size > _max_bytes():
        raise ValueError(
            f"архив больше {_max_bytes() // (1024 * 1024)} МБ")
    directory = root()
    os.makedirs(directory, exist_ok=True)
    disk.ensure_free(directory, size)
    upload_id = uuid.uuid4().hex
    fd, temp = tempfile.mkstemp(prefix=".part-", dir=directory)
    final = _path(upload_id)
    name = getattr(upload, "name", "") or "archive.zip"
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)
        os.replace(temp, final)
        _write_meta(upload_id, owner, name, size)
    except BaseException:
        for path in (temp, final):
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    prune()
    return {"id": upload_id, "name": name, "size": size,
            "owner": owner or "local"}


def resolve(upload_id: str) -> Optional[str]:
    """Путь загруженного архива; None — нет такого id (в т.ч. traversal)."""
    if not _valid_id(upload_id):
        return None
    path = _path(upload_id)
    return path if os.path.isfile(path) else None


def clear() -> None:
    shutil.rmtree(root(), ignore_errors=True)
