# -*- coding: utf-8 -*-
"""Системные операции: индексация и валидация задачи.

Это предпосылки тяжёлой работы. Результат привязан к отпечатку файлов:
пока видео не менялось, повторный экспорт переиспользует готовую
валидацию и не декодирует гигабайты заново. Заменили файл — отпечаток
другой, и проверка пойдёт снова.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Tuple

import workspace as ws_module
from vc_pairs import frame_provider

from . import checkpoints
from .registry import JobRegistry, JobType
from .scheduler import JobResult, JobSpec

INDEX_TYPE = "system.index"
VALIDATE_TYPE = "system.validate"

#: Типы, которым нужна свежая проверка видео перед запуском.
_VIDEO_CONSUMERS = frozenset({"export.fragments"})


def _roles(ws) -> List[Tuple[str, str]]:
    """Роли видео задачи в порядке проверки: источник, затем превью."""
    roles = []
    if ws.original:
        roles.append(("source", ws.original))
    if ws.visualization:
        roles.append(("preview", ws.visualization))
    return roles


def task_fingerprint(ws) -> str:
    """Отпечаток видео задачи: имена, размеры и mtime ролей.

    Читает только stat: гигабайты не декодируются. Отпечаток меняется,
    когда файл заменили или переписали, — тогда валидация не может быть
    переиспользована.
    """
    parts = []
    for role, path in _roles(ws):
        st = frame_provider.content_stamp(path)
        if st is None:
            parts.append(f"{role}:{os.path.basename(path)}:-")
        else:
            parts.append(
                f"{role}:{os.path.basename(path)}:{st[0]}:{st[1]}")
    if not parts:
        return "none"
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _task(ws_id: str):
    if not ws_id:
        raise RuntimeError("не указан id задачи")
    ws = ws_module.get_workspace(ws_id)
    if ws is None:
        raise RuntimeError(f"Задача '{ws_id}' не найдена")
    return ws


class _VideoProblem(RuntimeError):
    """Внешний сбой чтения видео: файл, диск, декодер.

    Такой сбой не отменяет уже прочитанные роли: контрольная точка остаётся,
    и повтор продолжит с проблемного файла.
    """


def _bad_video(ws_id: str, message: str, path: str = "") -> _VideoProblem:
    """Помечает задачу битой и возвращает ошибку для записи операции."""
    ws_module.note_workspace_error(ws_id, message, path)
    return _VideoProblem(message)


def _inspect(ws_id: str, ws, ctx, *,
             fingerprint: str = "") -> Dict[str, Any]:
    """Читает метаданные всех ролей; сбой привязан к конкретному файлу.

    Успешные роли сохраняются в контрольной точке по отпечатку файлов:
    внешний сбой на следующей роли не заставляет перечитывать предыдущие.
    """
    roles = _roles(ws)
    if not roles:
        raise _bad_video(
            ws_id, "Не удалось прочитать видео: видео не распознано "
                   "(расширение файла не видео)")

    key = fingerprint or task_fingerprint(ws)
    saved = checkpoints.load(ws_id, key)

    result: Dict[str, Any] = {"roles": {}}
    total = len(roles)
    for index, (role, path) in enumerate(roles, 1):
        ctx.checkpoint()
        ctx.progress(index - 1, total, message=f"Проверка {role}",
                     unit="файл")
        meta = saved.get(role)
        if meta is None:
            try:
                meta = frame_provider.get_metadata(path)
            except frame_provider._DecodeCancelled:
                raise
            except OSError as exc:
                raise _bad_video(
                    ws_id, f"Не удалось прочитать видео: {exc}", path)
            except Exception as exc:  # noqa: BLE001 — любой сбой чтения
                raise _bad_video(
                    ws_id, f"Не удалось прочитать видео: {exc}", path)
            checkpoints.save_role(ws_id, key, role, meta)
        if not meta.get("total_frames"):
            raise _bad_video(
                ws_id, "Не удалось прочитать видео: видео не содержит кадров",
                path)
        result["roles"][role] = meta
        ctx.progress(index, total, message=f"Проверка {role}", unit="файл")

    preview = result["roles"].get("preview")
    source = result["roles"].get("source")
    primary = preview or source or {}
    result["total_frames"] = primary.get("total_frames", 0)
    result["fps"] = primary.get("fps")
    if source and preview and \
            source.get("total_frames") != preview.get("total_frames"):
        result["warning"] = (
            f"число кадров не совпадает: источник "
            f"{source.get('total_frames')}, превью "
            f"{preview.get('total_frames')}")
    return result


def execute_index(job, ctx) -> JobResult:
    ws_id = job.target.get("id")
    ws = _task(ws_id)
    key = task_fingerprint(ws)
    try:
        data = _inspect(ws_id, ws, ctx, fingerprint=key)
    except frame_provider._DecodeCancelled:
        return JobResult(summary="Задача удалена во время индексации")
    except _VideoProblem:
        raise
    except BaseException:
        # Внутренняя ошибка — точка могла быть записана частично и уже
        # не описывает реальный прогресс; проверка начнётся заново.
        checkpoints.clear(ws_id, key)
        raise

    # Точку не убираем: следующая валидация той же версии переиспользует
    # прочитанные роли, не декодируя видео второй раз.
    ctx.progress(1, 1, message="Индекс готов", unit="задача")
    summary = f"Индекс: {data['total_frames']} кадров"
    return JobResult(summary=summary, extra={
        "task_id": ws_id,
        **data,
    })


def execute_validate(job, ctx) -> JobResult:
    ws_id = job.target.get("id")
    ws = _task(ws_id)
    key = task_fingerprint(ws)
    try:
        data = _inspect(ws_id, ws, ctx, fingerprint=key)
    except frame_provider._DecodeCancelled:
        return JobResult(summary="Задача удалена во время проверки")
    except _VideoProblem:
        raise
    except BaseException:
        checkpoints.clear(ws_id, key)
        raise

    checkpoints.clear(ws_id, key)
    # Успешная проверка — единственный способ снять старую отметку о
    # поломке без перезапуска процесса.
    ws_module.clear_workspace_error(ws_id)
    ctx.progress(1, 1, message="Проверка пройдена", unit="задача")
    extra = {"task_id": ws_id, "valid": True, **data}
    return JobResult(
        summary=f"Проверка: {data['total_frames']} кадров",
        extra=extra,
    )


def _index_prerequisite(spec) -> List[JobSpec]:
    ws_id = (spec.target or {}).get("id")
    ws = ws_module.get_workspace(ws_id) if ws_id else None
    if ws is None or not _roles(ws):
        return []
    key = task_fingerprint(ws)
    return [JobSpec(
        type=INDEX_TYPE,
        target=dict(spec.target),
        owner="system",
        priority=spec.priority,
        inherit_priority=True,
        idempotency_key=f"index:{key}",
    )]


def validation_prerequisites(spec) -> List[JobSpec]:
    """Свежая валидация для операций, читающих видео.

    Ключ идемпотентности — отпечаток файлов: готовая проверка той же
    версии задачи переиспользуется. Задачи нет — предпосылок нет, пусть
    исполнитель сам скажет «не найдена».
    """
    if spec.type not in _VIDEO_CONSUMERS:
        return []
    target = spec.target or {}
    if target.get("kind") != "task":
        return []
    ws = ws_module.get_workspace(target.get("id")) if target.get("id") else None
    if ws is None or not _roles(ws):
        return []
    key = task_fingerprint(ws)
    return [JobSpec(
        type=VALIDATE_TYPE,
        target=dict(target),
        owner="system",
        priority=spec.priority,
        inherit_priority=True,
        idempotency_key=f"validate:{key}",
    )]


def ensure_task_ready(task_id: str):
    """Поставить валидацию при открытии задачи (если планировщик запущен).

    Дедупликация по отпечатку делает повторные открытия бесплатными.
    Если процесс работает без очереди (тесты, внешний контур), просто
    ничего не делаем: открытие задачи не должно зависеть от неё.
    """
    import vc_jobs

    scheduler = vc_jobs.peek_scheduler()
    if scheduler is None or scheduler.stopping:
        return None
    ws = ws_module.get_workspace(task_id) if task_id else None
    if ws is None or not _roles(ws):
        return None
    try:
        job, _ = scheduler.submit(JobSpec(
            type=VALIDATE_TYPE,
            target={"kind": "task", "id": task_id},
            owner="system",
            priority=100,
            idempotency_key=f"validate:{task_fingerprint(ws)}",
        ))
    except (KeyError, ValueError, RuntimeError):
        return None
    return job


def register(registry: JobRegistry) -> None:
    registry.register(JobType(
        key=INDEX_TYPE,
        lane="system",
        label="Индексация видео",
        executor=execute_index,
        result_kind="object",
        cancellable=True,
        resumable=True,
        retention_days=None,
        record_retention_days=1.0,
    ))
    registry.register(JobType(
        key=VALIDATE_TYPE,
        lane="system",
        label="Проверка видео",
        executor=execute_validate,
        result_kind="object",
        cancellable=True,
        resumable=True,
        retention_days=None,
        record_retention_days=1.0,
        prerequisites=_index_prerequisite,
    ))
