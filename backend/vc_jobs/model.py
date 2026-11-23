# -*- coding: utf-8 -*-
"""Запись фоновой операции: поля, статусы, сериализация.

Модуль не знает ни про очередь, ни про HTTP. Запись — факт о задаче,
который живёт на диске; ``display_status`` добавляет только вычисляемое
«Отмена…», чтобы UI не хранил переходное состояние.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

#: Очередь и выполнение (не терминальные).
QUEUED = "queued"
RUNNING = "running"
#: Терминальные состояния.
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

STATUSES = frozenset({QUEUED, RUNNING, DONE, ERROR, CANCELLED, INTERRUPTED})
ACTIVE_STATUSES = frozenset({QUEUED, RUNNING})
TERMINAL_STATUSES = frozenset({DONE, ERROR, CANCELLED, INTERRUPTED})

#: Поля v1. Незнакомые ключи сохраняются в ``extra`` (расширяемость).
CORE_FIELDS = (
    "v", "id", "type", "lane", "status", "dedupe_key", "owner", "target",
    "params", "priority", "depends_on", "attempt", "progress", "result",
    "error", "cancel_requested", "hidden", "created_at", "started_at",
    "finished_at", "heartbeat_at", "retention",
)


class JobValidationError(ValueError):
    """Запись не соответствует схеме (битый JSON, чужой статус)."""


def now_iso() -> str:
    """UTC-время с миллисекундами — сортируемо и читаемо человеком."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Разбор ISO-времени; None/мусор — None (не роняем чтение)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise JobValidationError(message)


@dataclass
class Job:
    """Одна фоновая операция."""

    id: str
    type: str
    lane: str
    status: str
    dedupe_key: str
    owner: str
    target: Dict[str, Any]
    params: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    depends_on: List[str] = field(default_factory=list)
    attempt: int = 1
    progress: Dict[str, Any] = field(
        default_factory=lambda: {
            "current": 0, "total": 0, "unit": "", "message": ""})
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    cancel_requested: bool = False
    #: Убрана из рабочего списка вручную; в истории остаётся.
    hidden: bool = False
    created_at: str = field(default_factory=now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    retention: Dict[str, Any] = field(
        default_factory=lambda: {"expires_at": None, "keep": True})
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require(isinstance(self.id, str) and bool(self.id),
                 "id обязателен")
        _require(isinstance(self.type, str) and bool(self.type),
                 "type обязателен")
        _require(self.status in STATUSES,
                 f"неизвестный статус операции: {self.status!r}")
        _require(isinstance(self.target, dict), "target должен быть объектом")
        _require(bool(self.target.get("kind")) and bool(self.target.get("id")),
                 "target требует kind и id")

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def display_status(self) -> str:
        """Статус для UI: отмена во время работы показывается отдельно."""
        if self.status == RUNNING and self.cancel_requested:
            return "cancelling"
        return self.status

    def set_progress(self, current: int, total: int,
                     message: Optional[str] = None,
                     unit: Optional[str] = None) -> None:
        """Прогресс с зажимом в ``[0, total]``; отрицательное — ноль."""
        total = max(0, int(total))
        current = max(0, min(int(current), total))
        self.progress = {
            "current": current,
            "total": total,
            "unit": self.progress.get("unit", "") if unit is None else unit,
            "message": (self.progress.get("message", "")
                        if message is None else message),
        }

    def touch(self) -> None:
        """Отметка «исполнитель жив» (пишется вместе с прогрессом)."""
        self.heartbeat_at = now_iso()

    def to_dict(self) -> Dict[str, Any]:
        """Сериализация v1; незнакомые ключи не теряются."""
        core = {
            "v": 1,
            "id": self.id,
            "type": self.type,
            "lane": self.lane,
            "status": self.status,
            "dedupe_key": self.dedupe_key,
            "owner": self.owner,
            "target": dict(self.target),
            "params": dict(self.params),
            "priority": self.priority,
            "depends_on": list(self.depends_on),
            "attempt": self.attempt,
            "progress": dict(self.progress),
            "result": self.result,
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "hidden": self.hidden,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "retention": dict(self.retention),
        }
        # extra не может подменить поля ядра.
        return {**self.extra, **core}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        _require(isinstance(data, dict), "запись операции должна быть объектом")
        for name in ("id", "type", "lane", "status", "dedupe_key", "owner",
                     "target"):
            _require(name in data, f"нет обязательного поля {name!r}")
        target = data["target"]
        _require(isinstance(target, dict), "target должен быть объектом")
        _require(isinstance(target.get("kind"), str)
                 and bool(target.get("kind")), "target.kind обязателен")
        _require(isinstance(target.get("id"), str)
                 and bool(target.get("id")), "target.id обязателен")
        params = data.get("params") or {}
        progress = data.get("progress") or {}
        depends_on = data.get("depends_on") or []
        _require(isinstance(params, dict), "params должен быть объектом")
        _require(isinstance(progress, dict), "progress должен быть объектом")
        _require(isinstance(depends_on, list), "depends_on должен быть списком")
        _require(isinstance(data.get("attempt", 1), int),
                 "attempt должен быть числом")
        _require(isinstance(data.get("priority", 0), int),
                 "priority должен быть числом")

        known = set(CORE_FIELDS)
        job = cls(
            id=data["id"],
            type=data["type"],
            lane=data["lane"],
            status=data["status"],
            dedupe_key=data["dedupe_key"],
            owner=data["owner"],
            target=dict(target),
            params=dict(params),
            priority=data.get("priority", 0),
            depends_on=list(depends_on),
            attempt=data.get("attempt", 1),
            progress={
                "current": int(progress.get("current", 0)),
                "total": int(progress.get("total", 0)),
                "unit": str(progress.get("unit", "")),
                "message": str(progress.get("message", "")),
            },
            result=data.get("result"),
            error=data.get("error"),
            cancel_requested=bool(data.get("cancel_requested", False)),
            hidden=bool(data.get("hidden", False)),
            created_at=data.get("created_at") or now_iso(),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            heartbeat_at=data.get("heartbeat_at"),
            retention=data.get("retention") or {"expires_at": None,
                                                 "keep": True},
            extra={k: v for k, v in data.items() if k not in known},
        )
        return job
