# -*- coding: utf-8 -*-
"""Планировщик фоновых операций: полосы, приоритет, отмена, рестарт.

Один планировщик на процесс. Записи — на диске, активная очередь — в
памяти. Ёмкость полосы занимается до старта потока и освобождается в
``finally``; после каждого перехода планировщик добирает очередь.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .activity import ActivityLog
from .model import (
    CANCELLED,
    DONE,
    ERROR,
    INTERRUPTED,
    QUEUED,
    RUNNING,
    Job,
    now_iso,
    parse_iso,
)
from .registry import JobRegistry, JobType
from .store import JobStore

logger = logging.getLogger(__name__)

#: Глубина каскада предпосылок: защита от случайного бесконечного графа.
MAX_PREREQ_DEPTH = 4
PAGE_LIMIT_MAX = 500

LaneCapacity = int


class JobConflict(RuntimeError):
    """Действие недопустимо в текущем статусе операции."""


class JobCycleError(RuntimeError):
    """Предпосылки образуют цикл или слишком глубоки."""


class JobCancelled(Exception):
    """Исполнитель увидел отмену и обязан завершиться."""


class JobInterrupted(Exception):
    """Сервис останавливается; результат не публикуется."""


@dataclass(frozen=True)
class JobSpec:
    """Запрос на операцию: тип, объект, параметры, владелец."""

    type: str
    target: Dict[str, Any]
    params: Dict[str, Any] = field(default_factory=dict)
    owner: str = "local"
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    idempotency_key: Optional[str] = None
    #: Переопределение ключа дедупликации (варианты параметров).
    dedupe_key: Optional[str] = None
    attempt: int = 1
    #: Предпосылка наследует приоритет заказчика (каскад не голодает).
    inherit_priority: bool = False


@dataclass
class JobResult:
    """Нормализованный результат исполнителя."""

    summary: str = ""
    files: List[str] = field(default_factory=list)
    download_path: Optional[str] = None
    download_name: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"summary": self.summary}
        if self.files:
            out["files"] = list(self.files)
        if self.download_path:
            out["download_path"] = self.download_path
        if self.download_name:
            out["download_name"] = self.download_name
        out.update(self.extra)
        return out


class JobContext:
    """То, что видит исполнитель: прогресс и отмена."""

    def __init__(self, scheduler: "Scheduler", job: Job,
                 cancel_event: threading.Event) -> None:
        self._scheduler = scheduler
        self.job = job
        self._cancel = cancel_event

    def progress(self, current: int, total: int,
                 message: Optional[str] = None,
                 unit: Optional[str] = None) -> None:
        self._scheduler.progress(self.job, current, total,
                                 message=message, unit=unit)

    def cancelled(self) -> bool:
        return self._scheduler.stopping or self._cancel.is_set()

    def checkpoint(self) -> None:
        """Бросает управляющее исключение, если работу пора прекратить."""
        if self._scheduler.stopping:
            raise JobInterrupted()
        if self._cancel.is_set():
            raise JobCancelled()


class Scheduler:
    """Локальная очередь операций. Потокобезопасен."""

    def __init__(self, store: JobStore, registry: JobRegistry, *,
                 activity: Optional[ActivityLog] = None,
                 lane_capacities: Optional[Dict[str, int]] = None,
                 max_workers: int = 4,
                 progress_interval: float = 1.0,
                 retention_days: float = 30.0,
                 error_retention_days: float = 90.0,
                 prune_interval: float = 300.0,
                 maintenance: Optional[Callable[[List[Job]], None]] = None
                 ) -> None:
        self.store = store
        self.registry = registry
        self.activity = activity
        self._lane_capacities = dict(lane_capacities or {})
        self._max_workers = max(1, int(max_workers))
        self._progress_interval = max(0.0, float(progress_interval))
        self._retention_days = max(0.0, float(retention_days))
        self._error_retention_days = max(0.0, float(error_retention_days))
        self._prune_interval = max(0.0, float(prune_interval))
        self._maintenance = maintenance
        self._last_prune = 0.0

        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._running: Dict[str, Job] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._cancels: Dict[str, threading.Event] = {}
        self._lane_running: Dict[str, int] = {}
        self._last_progress: Dict[str, float] = {}
        self._order = 0
        self._order_by_id: Dict[str, int] = {}
        self._started = False
        self._stopping = False

    # ─── жизненный цикл ──────────────────────────────────────────────────

    @property
    def stopping(self) -> bool:
        return self._stopping

    def start(self) -> "Scheduler":
        """Загружает записи и восстанавливает очередь (идемпотентно)."""
        with self._lock:
            if self._started:
                return self
            self._started = True
            for job in self.store.all():
                self._jobs[job.id] = job
                self._order += 1
                self._order_by_id[job.id] = self._order
            # Восстановление — после загрузки всех записей: провал
            # предпосылки обязан увидеть зависимые операции.
            for job in list(self._jobs.values()):
                self._recover_locked(job)
            self._prune_locked()
            self._pump_locked()
            return self

    def _recover_locked(self, job: Job) -> None:
        job_type = self.registry.get(job.type)
        if job_type is None:
            if job.status in (QUEUED, RUNNING):
                job.status = ERROR
                job.error = {
                    "code": "unknown_type",
                    "message": f"Тип операции не зарегистрирован: {job.type}",
                }
                job.finished_at = now_iso()
                self._save_locked(job)
                self._fail_dependents_locked(
                    job.id, code="prerequisite_failed",
                    message=f"Тип операции не зарегистрирован: {job.type}")
            return
        if job.status == INTERRUPTED:
            # Штатная остановка сервиса пишет ``interrupted`` сама, не
            # оставляя ``running`` на диске. Возобновляемый тип обязан
            # встать обратно в очередь, иначе рестарт теряет работу.
            if job.cancel_requested:
                # Отмену запросили до остановки — возобновлять нельзя.
                job.status = CANCELLED
                job.finished_at = now_iso()
                self._save_locked(job)
                self._fail_dependents_locked(
                    job.id, code="prerequisite_cancelled",
                    message="Предпосылка отменена")
            elif job_type.resumable:
                job.status = QUEUED
                job.attempt += 1
                job.started_at = None
                job.finished_at = None
                job.heartbeat_at = None
                job.error = None
                self._save_locked(job)
            else:
                self._fail_dependents_locked(
                    job.id, code="prerequisite_failed",
                    message="Предпосылка прервана; запустите заново")
            return
        if job.status != RUNNING:
            return
        if job.cancel_requested:
            job.status = CANCELLED
            job.finished_at = now_iso()
            self._save_locked(job)
            self._fail_dependents_locked(
                job.id, code="prerequisite_cancelled",
                message="Предпосылка отменена")
            return
        if job_type.resumable:
            job.status = QUEUED
            job.attempt += 1
            job.started_at = None
            job.finished_at = None
            job.heartbeat_at = None
            job.error = None
        else:
            job.status = INTERRUPTED
            job.error = {
                "code": "interrupted",
                "message": "Прервано перезапуском; запустите заново",
            }
            job.finished_at = now_iso()
        self._save_locked(job)
        if job.status == INTERRUPTED:
            self._fail_dependents_locked(
                job.id, code="prerequisite_failed",
                message="Предпосылка прервана перезапуском; запустите заново")

    def stop(self) -> None:
        """Останавливает приём работы; текущие операции — ``interrupted``."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            for job in list(self._running.values()):
                if job.status != RUNNING:
                    continue
                job.status = INTERRUPTED
                job.error = {
                    "code": "interrupted",
                    "message": "Сервис остановлен; операция возобновится "
                               "после запуска, если тип это допускает",
                }
                job.finished_at = now_iso()
                self._save_locked(job)
                self._record("job.interrupted", job, job.error["message"])
            events = list(self._cancels.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        # Дожидаемся исполнителей: иначе их поздняя запись может попасть
        # в уже удалённый каталог теста/остановленного сервиса.
        for thread in threads:
            thread.join(timeout=5.0)

    def _ensure_started(self) -> None:
        if not self._started:
            self.start()

    # ─── постановка и дедупликация ───────────────────────────────────────

    def submit(self, spec: JobSpec) -> Tuple[Job, bool]:
        """Ставит операцию; возвращает ``(job, deduplicated)``."""
        job_type = self.registry.get(spec.type)
        if job_type is None:
            raise KeyError(f"неизвестный тип операции: {spec.type!r}")
        target = spec.target
        if not isinstance(target, dict) or not target.get("kind") \
                or not target.get("id"):
            raise ValueError("target требует kind и id")
        key = spec.dedupe_key or self._dedupe_key(spec)
        with self._lock:
            self._ensure_started()
            return self._submit_locked(spec, job_type, key, ())

    def _dedupe_key(self, spec: JobSpec) -> str:
        key = (f"{spec.type}:{spec.target.get('kind')}:"
               f"{spec.target.get('id')}:owner:{spec.owner}")
        if spec.idempotency_key:
            key += f":idem:{spec.idempotency_key}"
        return key

    def _submit_locked(self, spec: JobSpec, job_type: JobType, key: str,
                       ancestry: Tuple[str, ...]) -> Tuple[Job, bool]:
        if key in ancestry:
            raise JobCycleError("предпосылки образуют цикл")
        if len(ancestry) >= MAX_PREREQ_DEPTH:
            raise JobCycleError("слишком глубокая цепочка предпосылок")

        existing = self._find_existing_locked(
            key, include_terminal=bool(spec.idempotency_key))
        if existing is not None:
            return existing, True

        now = now_iso()
        job = Job(
            id=str(uuid.uuid4()),
            type=spec.type,
            lane=job_type.lane,
            status=QUEUED,
            dedupe_key=key,
            owner=spec.owner or "local",
            target=dict(spec.target),
            params=dict(spec.params),
            priority=int(spec.priority),
            depends_on=list(spec.depends_on),
            attempt=max(1, int(spec.attempt)),
            created_at=now,
            retention={
                "expires_at": None,
                "keep": job_type.retention_days is None,
            },
        )

        if job_type.prerequisites is not None:
            dependency_ids = []
            for dep_spec in job_type.prerequisites(spec) or []:
                if not isinstance(dep_spec, JobSpec):
                    raise ValueError("prerequisites должен возвращать JobSpec")
                dep_type = self.registry.get(dep_spec.type)
                if dep_type is None:
                    raise KeyError(
                        f"неизвестный тип предпосылки: {dep_spec.type!r}")
                if dep_spec.inherit_priority:
                    dep_spec = replace(
                        dep_spec, priority=int(spec.priority))
                dep_key = dep_spec.dedupe_key or self._dedupe_key(dep_spec)
                dep_job, _ = self._submit_locked(
                    dep_spec, dep_type, dep_key, ancestry + (key,))
                if dep_spec.inherit_priority \
                        and dep_job.status == QUEUED \
                        and dep_job.priority > spec.priority:
                    # Фоновая проверка уже стояла в очереди; пользователь
                    # пришёл за ней — поднимаем её до приоритета заказчика.
                    dep_job.priority = int(spec.priority)
                    self._save_locked(dep_job)
                dependency_ids.append(dep_job.id)
            job.depends_on = list(dict.fromkeys(
                job.depends_on + dependency_ids))

        self._save_locked(job)
        self._jobs[job.id] = job
        self._order += 1
        self._order_by_id[job.id] = self._order
        self._record("job.created", job, job_type.label)
        self._pump_locked()
        return job, False

    def _find_existing_locked(self, key: str,
                              *, include_terminal: bool) -> Optional[Job]:
        found = [
            job for job in self._jobs.values()
            if job.dedupe_key == key
            and (include_terminal or job.active)
        ]
        if not found:
            return None
        return max(found, key=lambda j: (j.created_at, j.id))

    # ─── выбор и запуск ──────────────────────────────────────────────────

    def _capacity(self, lane: str) -> int:
        return max(1, int(self._lane_capacities.get(lane, 1)))

    def _deps_done_locked(self, job: Job) -> bool:
        for dep_id in job.depends_on:
            dep = self._jobs.get(dep_id)
            if dep is None or dep.status != DONE:
                return False
        return True

    def _pump_locked(self) -> None:
        if self._stopping:
            return
        self._maybe_prune_locked()
        while len(self._running) < self._max_workers:
            ready = [
                job for job in self._jobs.values()
                if job.status == QUEUED
                and self._deps_done_locked(job)
                and self._lane_running.get(job.lane, 0) < self._capacity(job.lane)
            ]
            if not ready:
                return
            job = min(ready, key=lambda j: (
                j.priority, self._order_by_id.get(j.id, 0),
                j.created_at, j.id))
            self._start_locked(job)

    def _start_locked(self, job: Job) -> None:
        job.status = RUNNING
        job.started_at = now_iso()
        job.heartbeat_at = job.started_at
        job.error = None
        self._save_locked(job)
        self._running[job.id] = job
        self._lane_running[job.lane] = \
            self._lane_running.get(job.lane, 0) + 1
        event = threading.Event()
        self._cancels[job.id] = event
        thread = threading.Thread(
            target=self._execute, args=(job.id, event),
            name=f"vcjob-{job.type}-{job.id[:8]}", daemon=True)
        self._threads[job.id] = thread
        self._record("job.started", job, job.type)
        thread.start()

    def _execute(self, job_id: str, event: threading.Event) -> None:
        with self._lock:
            job = self._jobs[job_id]
        job_type = self.registry.get(job.type)
        ctx = JobContext(self, job, event)
        try:
            if job_type is None:
                raise RuntimeError(f"тип не зарегистрирован: {job.type}")
            raw = job_type.executor(job, ctx)
            result = _normalize_result(raw)
        except JobInterrupted:
            self._finish(job, INTERRUPTED, error={
                "code": "interrupted", "message": "Операция прервана"})
        except JobCancelled:
            self._finish(job, CANCELLED)
        except Exception as exc:  # noqa: BLE001 — любой сбой = статус error
            logger.exception("Операция %s завершилась ошибкой", job.id)
            self._finish(job, ERROR, error={
                "code": "executor_error",
                "message": str(exc) or exc.__class__.__name__,
            })
        else:
            if self._stopping or job.status == INTERRUPTED:
                self._finish(job, INTERRUPTED, error={
                    "code": "interrupted", "message": "Операция прервана"})
            elif job.cancel_requested or event.is_set():
                self._finish(job, CANCELLED)
            else:
                self._finish(job, DONE, result=result, job_type=job_type)
        finally:
            with self._lock:
                self._running.pop(job_id, None)
                self._threads.pop(job_id, None)
                self._cancels.pop(job_id, None)
                self._lane_running[job.lane] = max(
                    0, self._lane_running.get(job.lane, 1) - 1)
                self._last_progress.pop(job_id, None)
                self._pump_locked()

    def _finish(self, job: Job, status: str, *,
                result: Optional[JobResult] = None,
                error: Optional[Dict[str, Any]] = None,
                job_type: Optional[JobType] = None) -> None:
        with self._lock:
            # stop() мог уже зафиксировать interrupted — не перетираем.
            if job.status == INTERRUPTED and status != INTERRUPTED:
                status = INTERRUPTED
            job.status = status
            job.finished_at = now_iso()
            if result is not None:
                job.result = result.to_dict()
            if error is not None:
                job.error = error
            if status == DONE and result is not None \
                    and result.download_path and job_type is not None \
                    and job_type.retention_days is not None:
                # Исполнитель может знать точный срок (кэш уже жил до
                # операции); иначе TTL отсчитывается от завершения.
                expires = parse_iso(result.extra.get("expires_at"))
                if expires is None:
                    expires = datetime.now(timezone.utc) + timedelta(
                        days=float(job_type.retention_days))
                job.retention = {"expires_at": expires.isoformat(),
                                 "keep": False}
            self._save_locked(job)
            summary = result.summary if result is not None else ""
            if status == ERROR and error:
                summary = error.get("message", "")
            self._record(f"job.{status}", job, summary)
            current_type = job_type or self.registry.get(job.type)
            terminal_for_dependents = status in (CANCELLED, ERROR) or (
                status == INTERRUPTED and current_type is not None
                and not current_type.resumable)
            if terminal_for_dependents:
                code = ("prerequisite_cancelled" if status == CANCELLED
                        else "prerequisite_failed")
                message = ("Предпосылка отменена" if status == CANCELLED
                           else "Предпосылка завершилась ошибкой"
                           + (f": {summary}" if summary else ""))
                self._fail_dependents_locked(
                    job.id, code=code, message=message)

    # ─── прогресс ────────────────────────────────────────────────────────

    def progress(self, job: Job, current: int, total: int, *,
                 message: Optional[str] = None,
                 unit: Optional[str] = None) -> None:
        with self._lock:
            live = self._jobs.get(job.id)
            if live is None:
                return
            live.set_progress(current, total, message=message, unit=unit)
            live.touch()
            now = time.monotonic()
            last = self._last_progress.get(job.id, 0.0)
            if self._progress_interval <= 0 \
                    or now - last >= self._progress_interval:
                self._last_progress[job.id] = now
                self._save_locked(live)

    # ─── отмена и повтор ─────────────────────────────────────────────────

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.terminal:
                raise JobConflict("операция уже завершена")
            job.cancel_requested = True
            if job.status == QUEUED:
                job.status = CANCELLED
                job.finished_at = now_iso()
                self._save_locked(job)
                self._record("job.cancelled", job, "Отменено до запуска")
                self._fail_dependents_locked(job.id)
            else:
                event = self._cancels.get(job.id)
                if event is not None:
                    event.set()
                self._save_locked(job)
            self._pump_locked()
            return job

    def _fail_dependents_locked(self, failed_id: str, *,
                                code: str = "prerequisite_cancelled",
                                message: str = "Предпосылка отменена") -> None:
        for job in list(self._jobs.values()):
            if not job.active or failed_id not in job.depends_on:
                continue
            job.status = ERROR
            job.error = {"code": code, "message": message}
            job.finished_at = now_iso()
            self._save_locked(job)
            self._record("job.error", job, job.error["message"])
            self._fail_dependents_locked(
                job.id, code="prerequisite_failed",
                message="Предпосылка завершилась ошибкой")

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.active:
                raise JobConflict("операция ещё выполняется")
            retried, _ = self.submit(JobSpec(
                type=job.type,
                target=dict(job.target),
                params=dict(job.params),
                owner=job.owner,
                priority=job.priority,
                attempt=job.attempt + 1,
            ))
            return retried

    def set_retention(self, job_id: str, expires_at: str) -> Job:
        """Продлевает срок результата (скачивание архива)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.retention = {"expires_at": expires_at, "keep": False}
            self._save_locked(job)
            return job

    # ─── чтение ──────────────────────────────────────────────────────────

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            self._ensure_started()
            return self._jobs.get(job_id)

    def all(self) -> List[Job]:
        """Снимок всех известных операций (для сводки и истории)."""
        with self._lock:
            self._ensure_started()
            return list(self._jobs.values())

    def list(self, *, type: Optional[str] = None,
             status: Optional[str] = None, owner: Optional[str] = None,
             target: Optional[str] = None,
             project_id: Optional[str] = None,
             active: bool = False,
             limit: int = 100,
             cursor: Optional[str] = None) -> Tuple[List[Job], Optional[str]]:
        with self._lock:
            self._ensure_started()
            jobs = list(self._jobs.values())
        if type:
            jobs = [j for j in jobs if j.type == type]
        if status:
            jobs = [j for j in jobs
                    if j.status == status or j.display_status == status]
        if owner:
            jobs = [j for j in jobs if j.owner == owner]
        if target:
            kind, _, ident = target.partition(":")
            jobs = [j for j in jobs
                    if j.target.get("kind") == kind
                    and j.target.get("id") == ident]
        if project_id:
            jobs = [j for j in jobs
                    if j.target.get("project_id") == project_id]
        if active:
            jobs = [j for j in jobs if j.status in (QUEUED, RUNNING)]
        jobs.sort(key=lambda j: (j.created_at, j.id), reverse=True)
        if cursor:
            cursor_id = _decode_cursor(cursor)
            index = next((i for i, j in enumerate(jobs)
                          if j.id == cursor_id), None)
            if index is None:
                raise ValueError("неизвестный курсор")
            jobs = jobs[index + 1:]
        limit = max(1, min(PAGE_LIMIT_MAX, int(limit)))
        page = jobs[:limit]
        next_cursor = _encode_cursor(page[-1].id) if len(jobs) > limit else None
        return page, next_cursor

    def summary(self) -> Dict[str, int]:
        with self._lock:
            self._ensure_started()
            counts = {QUEUED: 0, RUNNING: 0, DONE: 0, ERROR: 0,
                      CANCELLED: 0, INTERRUPTED: 0}
            for job in self._jobs.values():
                counts[job.status] = counts.get(job.status, 0) + 1
            counts["active"] = counts[QUEUED] + counts[RUNNING]
            return counts

    def prune(self, now: Optional[datetime] = None) -> int:
        """Удаляет терминальные записи по сроку и зовёт обслуживание."""
        with self._lock:
            return self._prune_locked(now)

    def _maybe_prune_locked(self) -> None:
        if self._prune_interval <= 0:
            self._prune_locked()
            return
        if time.monotonic() - self._last_prune < self._prune_interval:
            return
        self._prune_locked()

    def _prune_locked(self, now: Optional[datetime] = None) -> int:
        moment = now or datetime.now(timezone.utc)
        self._last_prune = time.monotonic()
        removed = 0
        for job in list(self._jobs.values()):
            if not job.terminal:
                continue
            finished = parse_iso(job.finished_at)
            if finished is None:
                continue
            days = (self._error_retention_days if job.status == ERROR
                    else self._retention_days)
            if finished >= moment - timedelta(days=days):
                continue
            self.store.delete(job.id)
            self._jobs.pop(job.id, None)
            self._order_by_id.pop(job.id, None)
            removed += 1
        if self._maintenance is not None:
            try:
                self._maintenance(list(self._jobs.values()))
            except Exception:  # noqa: BLE001 — уборка не роняет очередь
                logger.exception("Обслуживание фоновых операций упало")
        return removed

    def _save_locked(self, job: Job) -> None:
        self.store.save(job)
        self._jobs[job.id] = job

    def _record(self, action: str, job: Job, summary: str = "") -> None:
        if self.activity is None:
            return
        try:
            self.activity.append(
                action, actor=job.owner, target=job.target,
                project_id=job.target.get("project_id"),
                job_id=job.id, summary=summary)
        except Exception:  # noqa: BLE001 — журнал не должен ронять работу
            logger.exception("Не удалось записать событие %s", action)


def _encode_cursor(job_id: str) -> str:
    return base64.urlsafe_b64encode(job_id.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> str:
    try:
        return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("битый курсор") from exc


def _normalize_result(raw: Any) -> JobResult:
    if raw is None:
        return JobResult()
    if isinstance(raw, JobResult):
        return raw
    if isinstance(raw, dict):
        known = {"summary", "files", "download_path", "download_name"}
        return JobResult(
            summary=str(raw.get("summary") or ""),
            files=list(raw.get("files") or []),
            download_path=raw.get("download_path"),
            download_name=raw.get("download_name"),
            extra={k: v for k, v in raw.items() if k not in known},
        )
    return JobResult(summary=str(raw))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def default_lane_capacities() -> Dict[str, int]:
    cores = os.cpu_count() or 4
    export_default = max(1, min(4, cores // 4))
    return {
        "export": _env_int("VC_EXPORT_WORKERS", export_default),
        "archive": _env_int("VC_ARCHIVE_WORKERS", 1),
        "import": _env_int("VC_IMPORT_WORKERS", 1),
        "system": _env_int("VC_INDEX_BG_THREADS", 1),
    }


def build_scheduler(state_root: str) -> Scheduler:
    """Собирает локальный планировщик с зарегистрированными типами VC."""
    from .builtin import register_builtin

    registry = JobRegistry()
    register_builtin(registry)
    activity = ActivityLog(
        os.path.join(state_root, "activity"),
        retention_days=_env_int("VC_ACTIVITY_RETENTION_DAYS", 90))
    store = JobStore(os.path.join(state_root, "jobs"))

    def maintenance(jobs):
        """Сроки хранения: журнал, загрузки, кэш архивов, контрольные точки."""
        from . import checkpoints, uploads
        from .archive_cache import ArchiveCache

        pinned = set()
        for job in jobs:
            if not job.active:
                continue
            if job.target.get("kind") == "upload":
                pinned.add(job.target.get("id"))
            upload_id = job.params.get("upload_id")
            if isinstance(upload_id, str) and upload_id:
                pinned.add(upload_id)
        uploads.prune(pinned=pinned)
        checkpoints.prune()
        archives = ArchiveCache(os.path.join(state_root, "archives"))
        archives.prune()
        archives.enforce_limit()
        activity.prune()

    return Scheduler(
        store, registry, activity=activity,
        lane_capacities=default_lane_capacities(),
        max_workers=_env_int("VC_JOBS_MAX", 4),
        retention_days=_env_float("VC_JOB_RETENTION_DAYS", 30.0),
        error_retention_days=_env_float("VC_JOB_ERROR_RETENTION_DAYS", 90.0),
        prune_interval=_env_float("VC_JOBS_PRUNE_INTERVAL_S", 300.0),
        maintenance=maintenance,
    )
