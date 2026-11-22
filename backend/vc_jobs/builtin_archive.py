# -*- coding: utf-8 -*-
"""Бэкапы и экспорт разметки как фоновые операции.

Исполнители не знают про HTTP: выбирают задачу/проект, считают отпечаток
состояния, переиспользуют совпавший архив или собирают новый в
``VC_STATE_ROOT/archives``. Скачивание отдаёт общий API операций.
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from typing import Any, Dict

import project_meta
import vc_jobs
import workspace as ws_module
from vc_fragments.annotations import write_annotations_archive
from vc_pairs.backup import (video_members, write_project_archive,
                             write_task_archive)
from workspace import get_workspace

from .archive_cache import ArchiveCache, CacheEntry
from .registry import JobRegistry, JobType
from .scheduler import JobResult

TASK_BACKUP_TYPE = "backup.task"
PROJECT_BACKUP_TYPE = "backup.project"
ANNOTATIONS_TYPE = "annotations.export"

TASK_TTL_DAYS = 7.0
PROJECT_TTL_DAYS = 7.0
ANNOTATIONS_TTL_DAYS = 1.0


def _stamp(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _task_state(ws) -> Dict[str, Any]:
    """Отпечаток содержимого задачи: паспорт, разметка, все видео файлы.

    Имена и mtime входят в отпечаток: заменили/переименовали файл — архив
    пересобирается. Чтения гигабайтов здесь нет, только stat.
    """
    files = [ws_module.FRAGMENTS_FILE, "task.json"]
    payload = {
        "id": os.path.basename(ws.path),
        "files": {name: _stamp(os.path.join(ws.path, name))
                  for name in files},
        "videos": {name: _stamp(os.path.join(ws.path, name))
                   for name in video_members(ws.path)},
    }
    return payload


def _project_state(project_id: str) -> Dict[str, Any]:
    tasks = sorted(
        name for name, pid in project_meta.task_project_ids().items()
        if pid == project_id)
    payload: Dict[str, Any] = {
        "id": project_id,
        "project": _stamp(os.path.join(
            project_meta.project_path(project_id), project_meta.PROJECT_FILE)),
        "tasks": [],
    }
    for name in tasks:
        ws = get_workspace(name)
        if ws is None:
            continue
        payload["tasks"].append(_task_state(ws))
    return payload


def _key(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _cache() -> ArchiveCache:
    return ArchiveCache(os.path.join(vc_jobs.state_root(), "archives"))


def _entry_result(entry: CacheEntry, *, summary: str,
                  **extra: Any) -> JobResult:
    out = {
        "cache": {
            "namespace": entry.namespace,
            "key": entry.key,
            "target": entry.target,
            "expires_at": entry.expires_at,
        },
        "expires_at": entry.expires_at,
    }
    out.update(extra)
    return JobResult(
        summary=summary,
        download_path=entry.archive_path,
        download_name=entry.download_name,
        extra=out,
    )


def _build(cache: ArchiveCache, namespace: str, key: str, target: str,
           download_name: str, ttl_days: float, writer, ctx) -> CacheEntry:
    """Собирает архив под замком версии; параллельная сборка ждёт готовый."""
    with cache.build_lock(namespace, key):
        existing = cache.lookup(namespace, key)
        if existing is not None:
            return existing

        temp = cache.new_temp(namespace)
        try:
            with zipfile.ZipFile(
                    temp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                writer(zf, ctx)
            entry = cache.commit(
                namespace, key, temp, target=target,
                download_name=download_name, ttl_days=ttl_days)
        except BaseException:
            try:
                os.remove(temp)
            except OSError:
                pass
            raise
        cache.remove_target(namespace, target, keep_key=key)
        cache.prune()
        return entry


def _task_backup(job, ctx) -> JobResult:
    ws_id = job.target.get("id")
    ws = get_workspace(ws_id) if ws_id else None
    if ws is None:
        raise RuntimeError(f"Задача '{ws_id}' не найдена")

    ctx.progress(0, 1, message="Сборка бэкапа", unit="архив")
    cache = _cache()
    key = _key(_task_state(ws))
    entry = _build(
        cache, TASK_BACKUP_TYPE, key, f"task:{ws_id}",
        f"{ws_id}-backup.zip", TASK_TTL_DAYS,
        lambda zf, context: write_task_archive(
            zf, ws, checkpoint=context.checkpoint),
        ctx)
    ctx.progress(1, 1, message="Готово", unit="архив")
    return _entry_result(entry, summary=f"Бэкап задачи {ws_id}",
                         task_id=ws_id)


def _project_backup(job, ctx) -> JobResult:
    project_id = job.target.get("id")
    if not project_id or not project_meta.exists(project_id):
        raise RuntimeError(f"Проект '{project_id}' не найден")

    ctx.progress(0, 1, message="Сборка бэкапа проекта", unit="архив")
    cache = _cache()
    key = _key(_project_state(project_id))
    entry = _build(
        cache, PROJECT_BACKUP_TYPE, key, f"project:{project_id}",
        f"{project_id}-backup.zip", PROJECT_TTL_DAYS,
        lambda zf, context: write_project_archive(
            zf, project_id, checkpoint=context.checkpoint),
        ctx)
    ctx.progress(1, 1, message="Готово", unit="архив")
    return _entry_result(entry, summary=f"Бэкап проекта {project_id}",
                         project_id=project_id)


def _annotations_export(job, ctx) -> JobResult:
    ws_id = job.target.get("id")
    ws = get_workspace(ws_id) if ws_id else None
    if ws is None:
        raise RuntimeError(f"Задача '{ws_id}' не найдена")
    with_media = bool(job.params.get("media"))

    ctx.progress(0, 1, message="Сборка архива разметки", unit="архив")
    cache = _cache()
    key = _key({"task": _task_state(ws), "media": with_media})
    entry = _build(
        cache, ANNOTATIONS_TYPE, key, f"task:{ws_id}",
        f"{ws_id}-annotations.zip", ANNOTATIONS_TTL_DAYS,
        lambda zf, context: write_annotations_archive(
            zf, ws, ws_id, with_media=with_media,
            checkpoint=context.checkpoint),
        ctx)
    ctx.progress(1, 1, message="Готово", unit="архив")
    return _entry_result(entry, summary=f"Разметка задачи {ws_id}",
                         task_id=ws_id)


def register(registry: JobRegistry) -> None:
    registry.register(JobType(
        key=TASK_BACKUP_TYPE,
        lane="archive",
        label="Бэкап задачи",
        executor=_task_backup,
        result_kind="file",
        cancellable=True,
        resumable=True,
        retention_days=TASK_TTL_DAYS,
    ))
    registry.register(JobType(
        key=PROJECT_BACKUP_TYPE,
        lane="archive",
        label="Бэкап проекта",
        executor=_project_backup,
        result_kind="file",
        cancellable=True,
        resumable=True,
        retention_days=PROJECT_TTL_DAYS,
    ))
    registry.register(JobType(
        key=ANNOTATIONS_TYPE,
        lane="archive",
        label="Экспорт разметки",
        executor=_annotations_export,
        result_kind="file",
        cancellable=True,
        resumable=True,
        retention_days=ANNOTATIONS_TTL_DAYS,
        params_schema={
            "media": {
                "type": "boolean",
                "default": False,
                "title": "Добавить видеофайлы",
            },
        },
    ))
