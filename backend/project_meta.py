# -*- coding: utf-8 -*-
"""Метаданные проекта VC (project.json) и реестр проектов.

Проект — отдельная сущность, как в CVAT: задача живёт в своей папке
(``workspaces/<task>/``) и указывает на проект полем ``project_id`` в
``task.json``. Состав проекта резолвится сканом указателей — списка
участников в ``project.json`` нет (единственный источник правды,
расхождение двух источников исключено).

Файлы::

    workspaces/projects/<pid>/project.json   — сам проект
    workspaces/.projects_migrated            — маркер разовой миграции

Без БД (stateless): чтение per-request, запись атомарно (tmp+rename),
памяти в ядре ноль. Формат и правила — docs/project-model.md.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import task_meta
from videocutter.standalone import workspace as ws_fs

logger = logging.getLogger(__name__)

PROJECT_FILE = "project.json"

#: Резервная папка в корне workspace-ов (сканнер её пропускает).
PROJECTS_DIRNAME = "projects"

#: Маркер разовой миграции старых задач в проект ``default``.
MIGRATION_MARKER = ".projects_migrated"

#: Проект для старых задач: создаётся один раз при первом скане.
DEFAULT_PROJECT_ID = "default"


# ─── Пути ────────────────────────────────────────────────────────────────────

def _root(root: Optional[str] = None) -> str:
    if root:
        return root
    import workspace  # лениво: избегаем цикла на импорте

    return workspace.WORKSPACE_ROOT


def projects_root(root: Optional[str] = None) -> str:
    """Папка реестра проектов внутри корня workspace-ов."""
    return os.path.join(_root(root), PROJECTS_DIRNAME)


def project_path(pid: str, root: Optional[str] = None) -> str:
    """Путь папки проекта с проверкой, что он внутри реестра."""
    return ws_fs.workspace_path(projects_root(root), pid)


# ─── Формат project.json ────────────────────────────────────────────────────

def defaults() -> Dict[str, Any]:
    """Пустые метаданные проекта (эквивалент отсутствующего файла)."""
    return {
        "id": None,
        "name": "",
        "created_at": None,
        # Точка расширения под будущую модель визуализации/AL: только
        # хранение, исполнения нет (см. docs/project-model.md).
        "model": {"id": None, "kind": "", "params": {}},
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate(meta: Dict[str, Any]) -> Dict[str, Any]:
    """Добивает дефолтами, проверяет типы. Чужие ключи сохраняет.

    :raises ValueError: name/id/model неверного типа.
    """
    out: Dict[str, Any] = dict(meta)
    name = out.get("name", "")
    if not isinstance(name, str):
        raise ValueError(f"Недопустимое name: {name!r}")
    pid = out.get("id")
    if pid is not None and (not isinstance(pid, str) or not pid.strip()):
        raise ValueError(f"Недопустимый id: {pid!r}")
    model = out.get("model")
    if model is not None and not isinstance(model, dict):
        raise ValueError(f"Недопустимый model: {model!r}")
    base = defaults()
    base.update(out)
    if base.get("model") is None:
        base["model"] = defaults()["model"]
    return base


def exists(pid: str, root: Optional[str] = None) -> bool:
    """Проект с таким id есть в реестре."""
    try:
        path = os.path.join(project_path(pid, root), PROJECT_FILE)
    except ws_fs.InvalidWorkspaceError:
        return False
    return os.path.isfile(path)


def load(pid: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Читает project.json; нет проекта — None, битый JSON — дефолты."""
    try:
        path = os.path.join(project_path(pid, root), PROJECT_FILE)
    except ws_fs.InvalidWorkspaceError:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("project.json не читается (%s): %s — дефолты", path, e)
        return defaults()
    if not isinstance(data, dict):
        logger.warning("project.json не объект (%s) — дефолты", path)
        return defaults()
    try:
        return validate(data)
    except ValueError as e:
        logger.warning("project.json невалиден (%s): %s — дефолты", path, e)
        return defaults()


def save(pid: str, meta: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    """Валидирует и пишет project.json атомарно (tmp+rename). Возвращает итог."""
    checked = validate(meta)
    checked["id"] = pid
    path = os.path.join(project_path(pid, root), PROJECT_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(checked, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return checked


def create(name: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Создаёт проект. Id = имя (одна папка, без путей) — как у задач.

    :raises ws_fs.InvalidWorkspaceError: имя некорректно.
    :raises ws_fs.WorkspaceExistsError: проект с таким id уже есть.
    """
    pid = ws_fs.sanitize_workspace_name(name)
    if exists(pid, root):
        raise ws_fs.WorkspaceExistsError(f"Проект '{pid}' уже существует")
    meta = defaults()
    meta["name"] = name.strip()
    meta["created_at"] = now_iso()
    return save(pid, meta, root)


def delete(pid: str, root: Optional[str] = None) -> None:
    """Удаляет папку проекта (задачи не трогает — их решает вызывающий)."""
    import shutil

    path = project_path(pid, root)
    if not os.path.isdir(path):
        raise ws_fs.InvalidWorkspaceError(f"Проект '{pid}' не найден")
    shutil.rmtree(path)


def list_projects(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Все проекты реестра (сортировка по имени). Битые — с warning, без падения."""
    base = projects_root(root)
    if not os.path.isdir(base):
        return []
    out: List[Dict[str, Any]] = []
    for entry in sorted(os.listdir(base)):
        if entry.startswith("."):
            continue
        if not os.path.isdir(os.path.join(base, entry)):
            continue
        meta = load(entry, root)
        if meta is None:
            continue
        meta["id"] = entry
        out.append(meta)
    out.sort(key=lambda m: (m.get("name") or m["id"]).lower())
    return out


# ─── Членство (указатель на задаче) ─────────────────────────────────────────

def task_project_ids(root: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Лёгкий скан: {имя задачи: project_id} по task.json (без индексов видео)."""
    base = _root(root)
    out: Dict[str, Optional[str]] = {}
    if not os.path.isdir(base):
        return out
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        if not os.path.isdir(full) or entry.startswith(".") \
                or entry == PROJECTS_DIRNAME:
            continue
        pid = task_meta.load(full).get("project_id")
        # Висячий указатель (проект вручную удалён) = standalone.
        out[entry] = pid if (pid and exists(pid, root)) else None
    return out


def resolve_pid(pid: Optional[str], root: Optional[str] = None) -> Optional[str]:
    """pid, если проект есть в реестре, иначе None (висячая привязка)."""
    return pid if (pid and exists(pid, root)) else None


def can_attach(task_id: str, pid: str, root: Optional[str] = None) -> Optional[str]:
    """Гейт привязки задачи к проекту: текст ошибки или None.

    Семантика снята с CVAT ``TaskWriteSerializer.update_project``: сейчас
    проверяются только существование сущностей; сюда же лягут будущие
    ограничения совместимости (метки задачи ⊆ меток проекта, размерность
    медиа/кодек под будущую модель) — точка расширения, не заглушка.
    """
    try:
        ws_fs.workspace_path(_root(root), task_id)
    except ws_fs.InvalidWorkspaceError:
        return f"Задача '{task_id}' не найдена"
    if task_id == PROJECTS_DIRNAME:
        return f"Задача '{task_id}' не найдена"  # реестр, не задача
    if not os.path.isdir(os.path.join(_root(root), task_id)):
        return f"Задача '{task_id}' не найдена"
    if not exists(pid, root):
        return f"Проект '{pid}' не найден"
    return None


def _save_membership(path: str, meta: Dict[str, Any]) -> None:
    """Пишет task.json, не двигая mtime папки задачи.

    Членство в проекте — не «работа»: сортировка «Недавние» опирается на
    mtime fragments.tsv, а у задачы без разметки — на mtime папки. Миграция
    и перевеска не должны переупорядочивать список.
    """
    try:
        st = os.stat(path)
    except OSError:
        st = None
    task_meta.save(path, meta)
    if st is not None:
        try:
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def attach(task_id: str, pid: str, root: Optional[str] = None) -> None:
    """Привязывает задачу к проекту (смена указателя в task.json)."""
    err = can_attach(task_id, pid, root)
    if err:
        raise ValueError(err)
    path = os.path.join(_root(root), task_id)
    meta = task_meta.load(path)
    meta["project_id"] = pid
    _save_membership(path, meta)


def detach(task_id: str, root: Optional[str] = None) -> None:
    """Делает задачу standalone (project_id = null)."""
    path = os.path.join(_root(root), task_id)
    if not os.path.isdir(path):
        raise ValueError(f"Задача '{task_id}' не найдена")
    meta = task_meta.load(path)
    if meta.get("project_id") is None:
        return
    meta["project_id"] = None
    _save_membership(path, meta)


def set_project_for_all(pid: Optional[str], root: Optional[str] = None,
                        only_null: bool = False) -> int:
    """Проставляет project_id всем задачам. Возвращает число изменений.

    ``only_null=True`` — только задачи без проекта (миграция в default).
    """
    changed = 0
    for name, current in task_project_ids(root).items():
        if only_null and current is not None:
            continue
        if current == pid:
            continue
        path = os.path.join(_root(root), name)
        meta = task_meta.load(path)
        meta["project_id"] = pid
        _save_membership(path, meta)
        changed += 1
    return changed


# ─── Разовая миграция ───────────────────────────────────────────────────────

def ensure_migrated(root: Optional[str] = None) -> None:
    """Первая загрузка существующей установки: все задачи → проект ``default``.

    Одноразовость ловится маркером ``.projects_migrated`` в корне: дальше
    «все без проекта» — уже осознанный standalone (явный detach), и скан
    его не трогает. Вызывается из сканнера workspace-ов; сбой ФС не должен
    ронять список задач — миграция просто повторится в следующий проход.
    """
    try:
        _ensure_migrated_impl(root)
    except Exception:  # noqa: BLE001 — bookkeeping не должен ломать скан
        logger.warning("миграция проектов не удалась", exc_info=True)


def _ensure_migrated_impl(root: Optional[str] = None) -> None:
    base = _root(root)
    if not os.path.isdir(base):
        return  # корня ещё нет (свежая установка) — маркер позже
    marker = os.path.join(base, MIGRATION_MARKER)
    if os.path.exists(marker):
        return

    tasks = task_project_ids(root)
    if tasks and not exists(DEFAULT_PROJECT_ID, root):
        try:
            create(DEFAULT_PROJECT_ID, root)
        except ws_fs.WorkspaceExistsError:
            pass
    if exists(DEFAULT_PROJECT_ID, root):
        moved = set_project_for_all(DEFAULT_PROJECT_ID, root, only_null=True)
        if moved:
            logger.info("миграция проектов: %d задач → '%s'",
                        moved, DEFAULT_PROJECT_ID)

    tmp = marker + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(now_iso() + "\n")
        os.replace(tmp, marker)
    except OSError as e:
        logger.warning("маркер миграции проектов не записан: %s", e)
