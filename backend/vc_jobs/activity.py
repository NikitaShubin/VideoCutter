# -*- coding: utf-8 -*-
"""Журнал активности: append-only JSONL без БД.

Один файл в сутки. Пишет только обвязка; содержимое — минимальные факты
(кто, что, когда, ссылка), без секретов и разметки. Grafana может читать
те же файлы, но обязательной зависимостью не является.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .model import now_iso, parse_iso

logger = logging.getLogger(__name__)


def _date_key(value: Optional[str]) -> str:
    parsed = parse_iso(value) or datetime.now(timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")


def target_link(target: Optional[Dict[str, Any]]) -> Optional[str]:
    """Относительная ссылка на объект операции (CVAT-подобные маршруты)."""
    if not target:
        return None
    kind = target.get("kind")
    ident = target.get("id")
    if not kind or not ident:
        return None
    if kind == "task":
        return f"/tasks/{ident}"
    if kind == "project":
        return f"/projects/{ident}"
    return None


class ActivityLog:
    """Суточные JSONL-файлы ``activity/YYYY-MM-DD.jsonl``."""

    def __init__(self, root: str, retention_days: int = 90) -> None:
        self.root = os.path.abspath(root)
        self.retention_days = int(retention_days)
        self._lock = threading.Lock()

    def _path(self, day: str) -> str:
        return os.path.join(self.root, day + ".jsonl")

    def append(self, action: str, *, actor: str = "local",
               target: Optional[Dict[str, Any]] = None,
               project_id: Optional[str] = None,
               job_id: Optional[str] = None, summary: str = "",
               link: Optional[str] = None,
               details: Optional[Dict[str, Any]] = None,
               ts: Optional[str] = None) -> Dict[str, Any]:
        event = {
            "ts": ts or now_iso(),
            "actor": actor,
            "action": action,
            "target": dict(target) if target else None,
            "project_id": project_id,
            "job_id": job_id,
            "summary": summary,
            "link": link or target_link(target),
        }
        if details:
            event["details"] = dict(details)
        day = _date_key(event["ts"])
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            path = self._path(day)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
        return event

    def read(self, *, from_: Optional[Any] = None, to: Optional[Any] = None,
             project_id: Optional[str] = None,
             task_id: Optional[str] = None,
             limit: int = 100) -> List[Dict[str, Any]]:
        """События новее ``from_`` и не позже ``to``, новые сверху."""
        start = _bound(from_)
        end = _bound(to)
        limit = max(1, int(limit))
        try:
            names = sorted(os.listdir(self.root), reverse=True)
        except OSError:
            return []
        events: List[Dict[str, Any]] = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(self.root, name)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in reversed(lines):
                event = self._parse(line)
                if event is None:
                    continue
                stamp = parse_iso(event.get("ts"))
                if stamp is None:
                    continue
                if start is not None and stamp < start:
                    continue
                if end is not None and stamp > end:
                    continue
                if project_id is not None \
                        and event.get("project_id") != project_id:
                    continue
                target = event.get("target") or {}
                if task_id is not None \
                        and not (target.get("kind") == "task"
                                 and target.get("id") == task_id):
                    continue
                events.append(event)
                if len(events) >= limit:
                    return events
        return events

    @staticmethod
    def _parse(line: str) -> Optional[Dict[str, Any]]:
        try:
            event = json.loads(line)
        except ValueError:
            logger.warning("Пропускаю битую строку журнала активности")
            return None
        return event if isinstance(event, dict) else None

    def prune(self, now: Optional[datetime] = None) -> int:
        """Удаляет суточные файлы старше ``retention_days``."""
        moment = now or datetime.now(timezone.utc)
        threshold = (moment - timedelta(days=self.retention_days)).date()
        removed = 0
        with self._lock:
            try:
                names = os.listdir(self.root)
            except OSError:
                return 0
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                try:
                    day = datetime.strptime(name[:-6], "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day >= threshold:
                    continue
                try:
                    os.unlink(os.path.join(self.root, name))
                except OSError:
                    continue
                removed += 1
        return removed


def _bound(value: Optional[Any]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return parse_iso(str(value))
