# -*- coding: utf-8 -*-
"""Экспорт нарезок как фоновая операция.

Исполнитель — тонкая обвязка: выбирает workspace, снимает устаревшие
нарезки, вызывает прежний проверенный конвейер и переводит его прогресс,
отмену и результат в ``JobContext``. Правила нарезки остаются в ядре.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from .registry import JobRegistry, JobType
from .scheduler import JobResult

EXPORT_TYPE = "export.fragments"


class _JobExportHooks:
    """Мост между конвейером экспорта и записью фоновой операции."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.files = []
        self.task_hash = None
        self.error = None
        self.was_cancelled = False

    def progress(self, ws_id: str, index: int, total: int) -> bool:
        self.ctx.progress(index, total, unit="фрагмент")
        return True

    def cancelled(self, ws_id: str) -> bool:
        return self.ctx.cancelled()

    def finished(self, ws_id: str, files, task_hash) -> None:
        # Sidecar и состояние LEDGER — общий контракт с прежним экспортом:
        # следующий запрос увидит готовый результат и не будет резать заново.
        from vc_fragments import views as export_views

        self.files = list(files)
        self.task_hash = task_hash
        export_views._export_finished_ok(ws_id, self.files, task_hash)

    def failed(self, ws_id: str, message: str) -> None:
        from vc_fragments import views as export_views

        self.error = message
        export_views._export_failed(ws_id, message)

    def cancelled_final(self, ws_id: str, created) -> None:
        # Пользователь просил отмену — частичные файлы этого запуска
        # удаляем, опубликованные не трогаем.
        from vc_fragments import views as export_views

        self.was_cancelled = True
        export_views._export_cancelled(ws_id, created)

    def clear(self, ws_id: str) -> None:
        from vc_fragments import views as export_views

        export_views.LEDGER.clear_cancel(ws_id)


def _already_done(ws_id: str):
    """Готовый экспорт под текущие границы и видео (None — нет)."""
    from vc_fragments import views as export_views

    return export_views._current_done(ws_id)


def _result_from_done(ws_id: str, done: Dict[str, Any]) -> JobResult:
    files = [item["filename"] for item in done.get("files") or []]
    urls = [item["url"] for item in done.get("files") or []]
    return JobResult(
        summary=f"Экспорт: {len(files)} нарезок",
        files=files,
        extra={"task_id": ws_id, "urls": urls},
    )


def execute(job, ctx) -> JobResult:
    """Выполняет экспорт задачи; прогресс и отмена — через ``ctx``."""
    from export_guard import START_LOCK
    from export_ledger import LEDGER
    from vc_fragments import views as export_views

    ws_id = job.target.get("id")
    if not ws_id:
        raise RuntimeError("не указан id задачи")

    ws = export_views.get_workspace(ws_id)
    if ws is None:
        raise RuntimeError(f"Workspace '{ws_id}' не найден")

    # Решение «кто стартует» — под общим замком со старым экспортом:
    # проверка чужого состояния и захват задачи атомарны. Сам прогон идёт
    # вне замка; второй участник увидит либо запись операции (running),
    # либо запись LEDGER.
    with START_LOCK:
        running = LEDGER.snapshot(ws_id)
        if running and running.get("state") in ("queued", "running"):
            raise RuntimeError(
                "Экспорт уже выполняется или ждёт в очереди для этой задачи")

        if not job.params.get("force"):
            done = _already_done(ws_id)
            if done is not None:
                return _result_from_done(ws_id, done)

        export_views._drop_previous_outputs(ws)
        hooks = _JobExportHooks(ctx)

    export_views._run_export(ws_id, hooks=hooks)

    if hooks.error:
        raise RuntimeError(hooks.error)
    if hooks.was_cancelled:
        return JobResult(summary="Экспорт отменён")

    files = [os.path.basename(path) for path in hooks.files]
    urls = [f"/api/v1/pairs/{ws_id}/export/{name}" for name in files]
    return JobResult(
        summary=f"Экспорт: {len(files)} нарезок",
        files=files,
        extra={
            "task_id": ws_id,
            "urls": urls,
            "task_hash": hooks.task_hash,
        },
    )


def register(registry: JobRegistry, *, prerequisites=None) -> None:
    registry.register(JobType(
        key=EXPORT_TYPE,
        lane="export",
        label="Экспорт нарезок",
        executor=execute,
        result_kind="files",
        cancellable=True,
        resumable=True,
        retention_days=None,
        prerequisites=prerequisites,
        params_schema={
            "force": {
                "type": "boolean",
                "default": False,
                "title": "Пересобрать даже готовое",
            },
        },
    ))
