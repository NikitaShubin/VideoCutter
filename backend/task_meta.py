# -*- coding: utf-8 -*-
"""Метаданные задачи VC (task.json): владелец, назначения, статус, стадия.

Sidecar рядом с fragments.tsv внутри workspace. Формат и правила —
docs/task-model.md. Без БД: чтение per-request, запись атомарно
(tmp+rename), памяти в ядре ноль. Старой задаче без файла соответствуют
дефолты — миграции нет.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TASK_FILE = "task.json"
SCHEMA_VERSION = 1

STATUSES = ("new", "in_progress", "completed", "rejected")
STAGES = ("annotation", "validation", "acceptance")
SUBSETS = ("train", "val", "test", None)


def defaults() -> Dict[str, Any]:
    """Пустые метаданные (эквивалент отсутствующего task.json)."""
    return {
        "v": SCHEMA_VERSION,
        "owner": None,
        "assignees": [],
        "status": "new",
        "stage": "annotation",
        "created_at": None,
        "status_changed_at": None,
        "status_changed_by": None,
        "notes": "",
        "subset": None,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, (str, int))]


def validate(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Добивает дефолтами, проверяет справочники. Чужые ключи сохраняет.

    :raises ValueError: status/stage/subset вне справочника.
    """
    out: Dict[str, Any] = dict(meta)
    if out.get("status", "new") not in STATUSES:
        raise ValueError(f"Недопустимый status: {out.get('status')!r}")
    if out.get("stage", "annotation") not in STAGES:
        raise ValueError(f"Недопустимая stage: {out.get('stage')!r}")
    if out.get("subset") not in SUBSETS:
        raise ValueError(f"Недопустимый subset: {out.get('subset')!r}")
    base = defaults()
    base.update(out)
    base["v"] = SCHEMA_VERSION
    base["assignees"] = _as_str_list(base.get("assignees"))
    if base.get("notes") is None:
        base["notes"] = ""
    return base


def load(ws_path: str) -> Dict[str, Any]:
    """Читает task.json; нет файла или битый JSON — дефолты."""
    path = os.path.join(ws_path, TASK_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return defaults()
    except (OSError, ValueError) as e:
        logger.warning("task.json не читается (%s): %s — дефолты", path, e)
        return defaults()
    if not isinstance(data, dict):
        logger.warning("task.json не объект (%s) — дефолты", path)
        return defaults()
    try:
        return validate(data)
    except ValueError as e:
        logger.warning("task.json невалиден (%s): %s — дефолты", path, e)
        return defaults()


def save(ws_path: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Валидирует и пишет task.json атомарно (tmp+rename). Возвращает итог."""
    checked = validate(meta)
    path = os.path.join(ws_path, TASK_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(checked, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return checked


def init_new(owner: Optional[str] = None) -> Dict[str, Any]:
    """Заготовка новой задачи: статус new, стадия annotation."""
    meta = defaults()
    meta["owner"] = owner
    meta["created_at"] = _now_iso()
    return meta


def set_status(meta: Dict[str, Any], status: str,
               by: Optional[str] = None) -> Dict[str, Any]:
    """Меняет статус + штамп смены. Возвращает тот же словарь."""
    if status not in STATUSES:
        raise ValueError(f"Недопустимый status: {status!r}")
    meta["status"] = status
    meta["status_changed_at"] = _now_iso()
    meta["status_changed_by"] = by
    return meta
