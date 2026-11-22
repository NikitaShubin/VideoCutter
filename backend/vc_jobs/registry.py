# -*- coding: utf-8 -*-
"""Реестр типов операций — единственная точка расширения.

Новый тип = ``JobType`` с исполнителем и полосой. Очередь, API и UI
читают только этот интерфейс, поэтому добавление типа их не меняет.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

#: Однородные полосы: смешивать разнородные операции нельзя.
LANES = ("export", "archive", "import", "system")
LANE_SET = frozenset(LANES)


class UnknownLane(ValueError):
    """Тип объявил полосу, которой нет в планировщике."""


@dataclass(frozen=True)
class JobType:
    """Декларация типа операции.

    ``executor(job, ctx)`` вызывается планировщиком (или внешней очередью
    VDO) и не знает, кто его запустил. ``prerequisites(spec)`` возвращает
    спецификации операций, которые должны завершиться раньше.
    """

    key: str
    lane: str
    label: str
    executor: Callable[[Any, Any], Any]
    result_kind: str = "none"
    cancellable: bool = True
    resumable: bool = True
    #: Срок жизни результата в днях; None — результат не протухает.
    retention_days: Optional[float] = None
    params_schema: Dict[str, Any] = field(default_factory=dict)
    prerequisites: Optional[Callable[[Any], Sequence[Any]]] = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("key типа операции обязателен")
        if self.lane not in LANE_SET:
            raise UnknownLane(f"неизвестная полоса: {self.lane!r}")
        if not callable(self.executor):
            raise ValueError("executor должен быть вызываемым")
        if self.prerequisites is not None and not callable(self.prerequisites):
            raise ValueError("prerequisites должен быть вызываемым")

    def manifest(self) -> Dict[str, Any]:
        """JSON-описание для UI и VDO; исполнитель наружу не отдаётся."""
        return {
            "key": self.key,
            "lane": self.lane,
            "label": self.label,
            "result_kind": self.result_kind,
            "cancellable": self.cancellable,
            "resumable": self.resumable,
            "retention_days": self.retention_days,
            "params_schema": dict(self.params_schema),
            "has_prerequisites": self.prerequisites is not None,
        }


class JobRegistry:
    """Именованный набор типов; потокобезопасен."""

    def __init__(self) -> None:
        self._types: Dict[str, JobType] = {}
        self._lock = threading.Lock()

    def register(self, job_type: JobType) -> None:
        with self._lock:
            if job_type.key in self._types:
                raise ValueError(
                    f"тип операции уже зарегистрирован: {job_type.key!r}")
            self._types[job_type.key] = job_type

    def get(self, key: str) -> Optional[JobType]:
        with self._lock:
            return self._types.get(key)

    def types(self) -> List[JobType]:
        with self._lock:
            return list(self._types.values())

    def manifest(self) -> List[Dict[str, Any]]:
        return [jt.manifest() for jt in
                sorted(self.types(), key=lambda t: t.key)]
