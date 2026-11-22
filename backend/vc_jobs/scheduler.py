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
from dataclasses import dataclass, field
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
                 progress_interval: float = 1.0) -> None:
        self.store = store
        self.registry = registry
        self.activity = activity
        self._lane_capacities = dict(lane_capacities or {})
        self._max_workers = max(1, int(max_workers))
        self._progress_interval = max(0.0, float(progress_interval))

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
                self._recover_locked(job)
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
            return
        if job.status != RUNNING:
            return
        if job.cancel_requested:
            job.status = CANCELLED
            job.finished_at = now_iso()
        elif job_type.resumable:
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
                dep_key = dep_spec.dedupe_key or self._dedupe_key(dep_spec)
                dep_job, _ = self._submit_locked(
                    dep_spec, dep_type, dep_key, ancestry + (key,))
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
                expires = datetime.now(timezone.utc) + timedelta(
                    days=float(job_type.retention_days))
                job.retention = {"expires_at": expires.isoformat(),
                                 "keep": False}
            self._save_locked(job)
            summary = result.summary if result is not None else ""
            if status == ERROR and error:
                summary = error.get("message", "")
            self._record(f"job.{status}", job, summary)

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

    def _fail_dependents_locked(self, failed_id: str) -> None:
        for job in list(self._jobs.values()):
            if not job.active or failed_id not in job.depends_on:
                continue
            job.status = ERROR
            job.error = {
                "code": "prerequisite_cancelled",
                "message": "Предпосылка отменена",
            }
            job.finished_at = now_iso()
            self._save_locked(job)
            self._record("job.error", job, job.error["message"])
            self._fail_dependents_locked(job.id)

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
    return Scheduler(
        store, registry, activity=activity,
        lane_capacities=default_lane_capacities(),
        max_workers=_env_int("VC_JOBS_MAX", 4),
    )
