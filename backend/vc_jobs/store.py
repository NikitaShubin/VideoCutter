# -*- coding: utf-8 -*-
"""Файловое хранилище записей операций.

Одна операция — один JSON-файл. Запись атомарная (tmp + fsync + replace),
поэтому оборванный процесс не оставляет полуфайл. Памяти нет: чтение
всегда с диска, это источник истины.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import List, Optional

from .model import Job, JobValidationError, parse_iso

logger = logging.getLogger(__name__)

#: Безопасное имя файла: UUID и тестовые id, но никаких ``..`` и ``/``.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class JobStore:
    """Каталог ``jobs/`` с записями; потокобезопасен."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self._lock = threading.Lock()

    def _path(self, job_id: str) -> Optional[str]:
        if not isinstance(job_id, str) or not _ID_RE.match(job_id):
            return None
        return os.path.join(self.root, job_id + ".json")

    def save(self, job: Job) -> None:
        path = self._path(job.id)
        if path is None:
            raise ValueError(f"недопустимый id операции: {job.id!r}")
        tmp = os.path.join(
            self.root,
            f".{job.id}.{os.getpid()}.{threading.get_ident()}.tmp")
        with self._lock:
            os.makedirs(self.root, exist_ok=True)
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(job.to_dict(), f, ensure_ascii=False,
                              sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def get(self, job_id: str) -> Optional[Job]:
        path = self._path(job_id)
        if path is None:
            return None
        return self._read(path)

    def _read(self, path: str) -> Optional[Job]:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return Job.from_dict(data)
        except FileNotFoundError:
            # Запись удалили между listdir и чтением — это не ошибка.
            return None
        except (OSError, ValueError, JobValidationError) as exc:
            logger.warning("Пропускаю битую запись операции %s: %s",
                           path, exc)
            return None

    def all(self) -> List[Job]:
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        jobs = []
        for name in names:
            if not name.endswith(".json"):
                continue
            job = self._read(os.path.join(self.root, name))
            if job is not None:
                jobs.append(job)
        jobs.sort(key=lambda j: (j.created_at, j.id))
        return jobs

    def delete(self, job_id: str) -> None:
        path = self._path(job_id)
        if path is None:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    def prune(self, before: datetime) -> int:
        """Удаляет терминальные записи, завершённые раньше ``before``."""
        removed = 0
        with self._lock:
            for job in self.all():
                if not job.terminal:
                    continue
                finished = parse_iso(job.finished_at)
                if finished is None or finished >= before:
                    continue
                try:
                    os.unlink(os.path.join(self.root, job.id + ".json"))
                except OSError:
                    continue
                removed += 1
        return removed
