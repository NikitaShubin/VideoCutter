# -*- coding: utf-8 -*-
"""Фоновые операции VideoCutter: очередь, статусы, история.

Граница: пакет принадлежит обвязке (backend), ядро его не импортирует.
Публичный интерфейс — ``get_scheduler`` и типы из ``vc_jobs.model``.
"""

from __future__ import annotations

import os
import threading

_scheduler = None
_scheduler_lock = threading.Lock()


def state_root() -> str:
    """Корень состояния: env ``VC_STATE_ROOT`` или ``<workspaces>/.vc-state``."""
    explicit = os.getenv("VC_STATE_ROOT")
    if explicit:
        return os.path.abspath(explicit)
    root = os.getenv("VC_WORKSPACE_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "workspaces")
    return os.path.join(os.path.abspath(root), ".vc-state")


def get_scheduler():
    """Ленивый процесс-синглтон (импорт не создаёт каталогов и потоков)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            from .scheduler import build_scheduler

            _scheduler = build_scheduler(state_root())
        return _scheduler


def peek_scheduler():
    """Уже созданный планировщик без побочных эффектов (None — ещё нет)."""
    return _scheduler


def active_jobs(*, type=None, target=None):
    """Активные операции из планировщика или с диска.

    Нужно обвязке, которая обязана увидеть чужой запуск до старта: старый
    экспорт и ``export.fragments`` не должны резать одну задачу параллельно.
    """
    scheduler = peek_scheduler()
    if scheduler is not None:
        jobs = scheduler.all()
    else:
        from .store import JobStore

        jobs = JobStore(os.path.join(state_root(), "jobs")).all()
    return [
        job for job in jobs
        if job.active
        and (type is None or job.type == type)
        and (target is None or
             f"{job.target.get('kind')}:{job.target.get('id')}" == target)
    ]


def set_scheduler(scheduler) -> None:
    """Подмена для тестов; ``None`` — сбросить синглтон."""
    global _scheduler
    with _scheduler_lock:
        _scheduler = scheduler
