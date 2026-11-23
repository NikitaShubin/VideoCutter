# -*- coding: utf-8 -*-
"""Кэш архивов: переиспользование готового zip по отпечатку состояния.

Архивы бэкапов и разметки — производные данные. Их можно удалить и
собрать заново, поэтому кэш живёт вне папки задачи и чистится по TTL.
Совпал отпечаток — отдаём готовый файл, не читая гигабайты видео; состояние
изменилось — собираем новый и убираем прежнюю версию этого объекта.

TTL отсчитывается от последнего скачивания (как в CVAT): выдача обновляет
``expires_at`` и mtime файла. Истёкшую запись не отдаём — сборка начнётся
заново. Память не хранит фактов: источник истины — metadata.json рядом с
архивом; ``prune`` может работать после рестарта.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import disk

_CONTEXT_LOCK = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}


def _max_bytes() -> int:
    """Предел кэша в байтах; 0 — без предела (значение по умолчанию)."""
    try:
        mb = int(os.getenv("VC_ARCHIVE_CACHE_MAX_MB", "0"))
    except ValueError:
        mb = 0
    return max(0, mb) * 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    # Микросекунды, а не миллисекунды: порядок вытеснения сравнивает
    # timestamps строкой, и три архива, собранные подряд, не должны
    # становиться «одновременными» и решаться алфавитом ключей.
    return value.isoformat(timespec="microseconds")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class CacheEntry:
    namespace: str
    key: str
    target: str
    archive_path: str
    metadata_path: str
    download_name: str
    created_at: str
    expires_at: str
    last_access: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "v": 1,
            "namespace": self.namespace,
            "key": self.key,
            "target": self.target,
            "archive": os.path.basename(self.archive_path),
            "download_name": self.download_name,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_access": self.last_access,
        }


def _locked(path: str):
    with _CONTEXT_LOCK:
        lock = _LOCKS.setdefault(path, threading.Lock())
    return lock


class ArchiveCache:
    """Каталог ``<root>/<namespace>/<key>.zip`` + ``.json`` метаданные."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def _dir(self, namespace: str) -> str:
        safe = namespace.replace(os.sep, "_").replace("..", "_")
        return os.path.join(self.root, safe)

    def new_temp(self, namespace: str) -> str:
        """Временный файл сборки рядом с будущим архивом (rename без копии)."""
        directory = self._dir(namespace)
        os.makedirs(directory, exist_ok=True)
        disk.ensure_free(directory)
        fd, path = tempfile.mkstemp(
            prefix=".build-", suffix=".zip", dir=directory)
        os.close(fd)
        return path

    def _paths(self, namespace: str, key: str):
        base = os.path.join(self._dir(namespace), key)
        return base + ".zip", base + ".json"

    def _read(self, namespace: str, key: str) -> Optional[CacheEntry]:
        archive, metadata = self._paths(namespace, key)
        try:
            with open(metadata, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("key") != key:
            return None
        name = data.get("archive")
        if not isinstance(name, str) or os.path.basename(name) != name:
            return None
        archive = os.path.join(self._dir(namespace), name)
        if not os.path.isfile(archive):
            return None
        return CacheEntry(
            namespace=namespace,
            key=key,
            target=str(data.get("target") or ""),
            archive_path=archive,
            metadata_path=metadata,
            download_name=str(data.get("download_name") or name),
            created_at=str(data.get("created_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            last_access=str(data.get("last_access") or ""),
        )

    def lookup(self, namespace: str, key: str) -> Optional[CacheEntry]:
        """Живая запись кэша (None — нет, битая или просрочена)."""
        entry = self._read(namespace, key)
        if entry is None:
            return None
        expires = _parse(entry.expires_at)
        if expires is None or expires <= _now():
            self._remove(entry)
            return None
        return entry

    @contextmanager
    def build_lock(self, namespace: str, key: str):
        """Сериализует сборку одной версии между потоками процесса."""
        archive, _ = self._paths(namespace, key)
        with _locked(archive):
            yield

    def _write(self, entry: CacheEntry) -> None:
        os.makedirs(os.path.dirname(entry.metadata_path), exist_ok=True)
        tmp = entry.metadata_path + f".tmp-{os.getpid()}-{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, entry.metadata_path)

    def commit(self, namespace: str, key: str, temp_path: str, *,
               target: str, download_name: str,
               ttl_days: float) -> CacheEntry:
        """Публикует собранный файл атомарным rename и пишет метаданные."""
        archive, metadata = self._paths(namespace, key)
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        os.replace(temp_path, archive)
        now = _now()
        entry = CacheEntry(
            namespace=namespace,
            key=key,
            target=target,
            archive_path=archive,
            metadata_path=metadata,
            download_name=download_name,
            created_at=_iso(now),
            expires_at=_iso(now + timedelta(days=float(ttl_days))),
            last_access=_iso(now),
        )
        try:
            self._write(entry)
        except BaseException:
            try:
                os.remove(archive)
            except OSError:
                pass
            raise
        self.enforce_limit(keep=archive)
        return entry

    def store(self, namespace: str, key: str, data: bytes, download_name: str,
              *,
              target: str = "",
              ttl_days: float = 1.0) -> CacheEntry:
        """Кладёт готовые байты (тесты и небольшие архивы)."""
        directory = self._dir(namespace)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".build-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return self.commit(
                namespace, key, tmp, target=target,
                download_name=download_name, ttl_days=ttl_days)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def touch(self, namespace: str, key: str,
              ttl_days: float) -> Optional[CacheEntry]:
        """Продлевает TTL от текущего момента (скачивание/выдача)."""
        entry = self.lookup(namespace, key)
        if entry is None:
            return None
        now = _now()
        updated = CacheEntry(
            namespace=entry.namespace,
            key=entry.key,
            target=entry.target,
            archive_path=entry.archive_path,
            metadata_path=entry.metadata_path,
            download_name=entry.download_name,
            created_at=entry.created_at,
            expires_at=_iso(now + timedelta(days=float(ttl_days))),
            last_access=_iso(now),
        )
        self._write(updated)
        try:
            os.utime(updated.archive_path, None)
        except OSError:
            pass
        return updated

    def _remove(self, entry: CacheEntry) -> None:
        for path in (entry.archive_path, entry.metadata_path):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def remove(self, namespace: str, key: str) -> bool:
        """Удаляет одну запись кэша: архив и метаданные.

        Нужна ручной чистке из списка операций. Битые метаданные не
        мешают убрать файл: ключ проверяется, поэтому выйти за каталог
        кэша нельзя.
        """
        if not isinstance(key, str) or not key \
                or os.path.basename(key) != key or key in (".", ".."):
            return False
        entry = self._read(namespace, key)
        if entry is not None:
            self._remove(entry)
            return True
        archive, metadata = self._paths(namespace, key)
        removed = False
        for path in (archive, metadata):
            try:
                os.remove(path)
                removed = True
            except FileNotFoundError:
                pass
            except OSError:
                pass
        return removed

    def remove_target(self, namespace: str, target: str,
                      keep_key: Optional[str] = None) -> List[str]:
        """Убирает прежние версии объекта после успешной пересборки."""
        removed = []
        for entry in self._entries(namespace):
            if entry.target != target:
                continue
            if keep_key is not None and entry.key == keep_key:
                continue
            self._remove(entry)
            removed.append(entry.archive_path)
        return removed

    def _entries(self, namespace: Optional[str] = None) -> Iterable[CacheEntry]:
        namespaces = ([namespace] if namespace is not None
                      else [entry for entry in self._namespaces()])
        for ns in namespaces:
            directory = self._dir(ns)
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                key = name[:-len(".json")]
                entry = self._read(ns, key)
                if entry is not None:
                    yield entry

    def _namespaces(self) -> List[str]:
        try:
            return [name for name in os.listdir(self.root)
                    if os.path.isdir(os.path.join(self.root, name))]
        except OSError:
            return []

    def enforce_limit(self, keep: Optional[str] = None,
                      max_bytes: Optional[int] = None) -> List[str]:
        """Удаляет самые старые архивы, пока кэш не влезет в лимит.

        ``keep`` (только что собранный файл) не удаляется даже если один
        архив больше лимита: иначе операция вернула бы уже стёртый файл.
        Лимит ``0`` — без предела: кэш чистится только по TTL и вручную.
        """
        limit = _max_bytes() if max_bytes is None else max(0, int(max_bytes))
        if limit == 0:
            return []
        entries = list(self._entries())
        sizes = {}
        total = 0
        for entry in entries:
            try:
                sizes[entry.archive_path] = os.path.getsize(
                    entry.archive_path)
            except OSError:
                sizes[entry.archive_path] = 0
            total += sizes[entry.archive_path]
        if total <= limit:
            return []

        removed = []
        ordered = sorted(entries, key=lambda item: (
            item.last_access, item.created_at, item.key))
        for entry in ordered:
            if total <= limit:
                break
            if keep is not None \
                    and os.path.abspath(entry.archive_path) == os.path.abspath(
                        keep):
                continue
            self._remove(entry)
            total -= sizes.get(entry.archive_path, 0)
            removed.append(entry.archive_path)
        return removed

    def prune(self, now: Optional[datetime] = None) -> List[str]:
        """Удаляет просроченные архивы и осиротевшие временные файлы."""
        now = now or _now()
        removed = []
        for namespace in self._namespaces():
            directory = self._dir(namespace)
            for entry in list(self._entries(namespace)):
                expires = _parse(entry.expires_at)
                if expires is None or expires <= now:
                    self._remove(entry)
                    removed.append(entry.archive_path)
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not name.startswith(".build-"):
                    continue
                path = os.path.join(directory, name)
                try:
                    if now.timestamp() - os.path.getmtime(path) > 86400:
                        os.remove(path)
                        removed.append(path)
                except OSError:
                    pass
        return removed

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
