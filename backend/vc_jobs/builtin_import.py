# -*- coding: utf-8 -*-
"""Импорт задачи/проекта и разметки как фоновые операции.

Архив уже лежит в ``VC_STATE_ROOT/uploads`` (см. ``vc_jobs.uploads``).
Исполнитель только открывает файл и вызывает общий путь разбора — тот же,
что у старых HTTP-эндпоинтов. Распаковка идёт в стейджинг workspace-ов,
поэтому отмена не оставляет полузаписанную задачу.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import backup_format as fmt
from vc_fragments.annotations import AnnotationsProblem, import_annotations
from vc_pairs.backup import _Problem, import_archive
from workspace import get_workspace

from . import uploads
from .registry import JobRegistry, JobType
from .scheduler import JobResult

BACKUP_IMPORT_TYPE = "backup.import"
ANNOTATIONS_IMPORT_TYPE = "annotations.import"


def _upload_path(upload_id: str) -> str:
    path = uploads.resolve(upload_id)
    if path is None:
        raise RuntimeError(f"загруженный файл не найден: {upload_id}")
    return path


def _backup_import(job, ctx) -> JobResult:
    upload_id = job.target.get("id")
    path = _upload_path(upload_id)
    on_conflict = job.params.get("on_conflict", "rename")
    project = job.params.get("project") or None

    ctx.progress(0, 1, message="Импорт архива", unit="архив")
    with open(path, "rb") as fh:
        try:
            result = import_archive(
                fh, on_conflict=on_conflict, project_override=project,
                checkpoint=ctx.checkpoint)
        except _Problem as exc:
            raise RuntimeError(exc.message)
    ctx.progress(1, 1, message="Готово", unit="архив")

    extra: Dict[str, Any] = {"kind": result.get("kind")}
    if result.get("kind") == fmt.KIND_BACKUP \
            and result.get("tasks") is not None:
        extra["project_id"] = result["id"]
        extra["tasks"] = result["tasks"]
        summary = f"Импорт проекта {result['id']}"
    else:
        extra["task_id"] = result["id"]
        if result.get("project_id") is not None:
            extra["project_id"] = result["project_id"]
        summary = f"Импорт задачи {result['id']}"
    return JobResult(summary=summary, extra=extra)


def _annotations_import(job, ctx) -> JobResult:
    task_id = job.target.get("id")
    ws = get_workspace(task_id) if task_id else None
    if ws is None:
        raise RuntimeError(f"Задача '{task_id}' не найдена")
    upload_id = job.params.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id:
        raise RuntimeError("upload_id обязателен: сначала загрузите архив")
    path = _upload_path(upload_id)
    force = bool(job.params.get("force"))

    ctx.progress(0, 1, message="Импорт разметки", unit="архив")
    with open(path, "rb") as fh:
        try:
            result = import_annotations(
                ws, fh, force=force, checkpoint=ctx.checkpoint)
        except AnnotationsProblem as exc:
            raise RuntimeError(exc.message)
    ctx.progress(1, 1, message="Готово", unit="архив")
    return JobResult(
        summary=f"Разметка задачи {task_id}: {result['fragments']} фрагментов",
        extra={
            "task_id": task_id,
            "fragments": result["fragments"],
            "position": result["position"],
        },
    )


def register(registry: JobRegistry) -> None:
    registry.register(JobType(
        key=BACKUP_IMPORT_TYPE,
        lane="import",
        label="Импорт задачи или проекта",
        executor=_backup_import,
        result_kind="object",
        cancellable=True,
        resumable=False,
        retention_days=None,
        params_schema={
            "on_conflict": {
                "type": "string",
                "enum": ["error", "rename", "overwrite"],
                "default": "rename",
                "title": "При совпадении имён",
            },
            "project": {
                "type": "string",
                "default": "",
                "title": "Привязать задачу к проекту",
            },
        },
    ))
    registry.register(JobType(
        key=ANNOTATIONS_IMPORT_TYPE,
        lane="import",
        label="Импорт разметки",
        executor=_annotations_import,
        result_kind="object",
        cancellable=True,
        resumable=False,
        retention_days=None,
        params_schema={
            "upload_id": {
                "type": "string",
                "title": "Идентификатор загруженного архива",
            },
            "force": {
                "type": "boolean",
                "default": False,
                "title": "Игнорировать несовпадение видео",
            },
        },
    ))
