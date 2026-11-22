# -*- coding: utf-8 -*-
"""Контрольные точки длинных проверок (индекс/валидация).

Проверка видео читает роли по очереди. Если вторая роль падает по внешней
причине (диск, декодер), повторный запуск не обязан заново декодировать
первую: результат каждой роли сохраняется на диск и привязан к отпечатку
файлов. Отпечаток изменился — точка не годится, проверка идёт сначала.

Память не хранит фактов: источник истины — JSON-файл в
``VC_STATE_ROOT/checkpoints``. Файл переживает рестарт процесса, поэтому
ручной повтор после внешнего сбоя продолжает с места обрыва.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, Optional

import vc_jobs

_DIR = "checkpoints"
_LOCK = threading.Lock()
_DEFAULT_MAX_AGE_S = 7 * 86400


def root() -> str:
    return os.path.join(vc_jobs.state_root(), _DIR)


def _path(task_id: str, fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"{task_id}\0{fingerprint}".encode("utf-8")).hexdigest()
    return os.path.join(root(), digest + ".json")


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(
        prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def load(task_id: str, fingerprint: str) -> Dict[str, Dict[str, Any]]:
    """Успешные роли этой версии задачи; пусто — точки нет."""
    path = _path(task_id, fingerprint)
    with _LOCK:
        data = _read(path)
    if data is None:
        return {}
    if data.get("task_id") != task_id \
            or data.get("fingerprint") != fingerprint:
        return {}
    roles = data.get("roles")
    if not isinstance(roles, dict):
        return {}
    return {name: meta for name, meta in roles.items()
            if isinstance(name, str) and isinstance(meta, dict)}


def save_role(task_id: str, fingerprint: str, role: str,
              metadata: Dict[str, Any]) -> None:
    """Запоминает успешную роль; сбой записи не роняет проверку."""
    if not isinstance(metadata, dict):
        return
    path = _path(task_id, fingerprint)
    with _LOCK:
        data = _read(path) or {}
        if data.get("task_id") != task_id \
                or data.get("fingerprint") != fingerprint:
            data = {"task_id": task_id, "fingerprint": fingerprint,
                    "roles": {}}
        roles = data.get("roles")
        if not isinstance(roles, dict):
            roles = {}
        roles[role] = metadata
        data["roles"] = roles
        data["updated_at"] = time.time()
        try:
            _write(path, data)
        except OSError:
            pass


def clear(task_id: str, fingerprint: str) -> None:
    path = _path(task_id, fingerprint)
    with _LOCK:
        try:
            os.remove(path)
        except OSError:
            pass


def prune(max_age_s: Optional[float] = None) -> int:
    """Убирает точки старше срока и недописанные временные файлы."""
    directory = root()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    limit = time.time() - (_DEFAULT_MAX_AGE_S if max_age_s is None
                           else max_age_s)
    removed = 0
    with _LOCK:
        for name in names:
            if not (name.endswith(".json") or name.startswith(".tmp-")):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) >= limit:
                    continue
                os.remove(path)
            except OSError:
                continue
            removed += 1
    return removed


def clear_all() -> None:
    """Удаляет все точки (тесты, обслуживание)."""
    directory = root()
    with _LOCK:
        try:
            names = os.listdir(directory)
        except OSError:
            return
        for name in names:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
