# -*- coding: utf-8 -*-
"""Потоковый приём загрузок: стейджинг вместо Django-temp.

Классический multipart-путь держит до 3 копий файла (nginx-буфер +
Django-temp на другой ФС + копия в workspace) и не переживает обрыв.
Этот хендлер (первый в FILE_UPLOAD_HANDLERS) пишет чанки сразу в
стейджинг-каталог на ТОЙ ЖЕ файловой системе, что и workspaces;
финал — os.replace (мгновенно, без второй копии). Недокачанное чистится
прерыванием и sweep-ом протухшего. Ядро не тронуто: file_complete отдаёт
обычный UploadedFile с доп. staged_path, размещение решает lib
(см. _place_stream в standalone.workspace).

Staging: <WORKSPACE_ROOT>/.incoming/<uuid>/<field> (точка — сканнер
и классификатор его не видят). TTL недокачанного — 6 часов.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from typing import BinaryIO, Optional, Tuple

from django.core.files.uploadedfile import UploadedFile
from django.core.files.uploadhandler import (
    FileUploadHandler,
    StopFutureHandlers,
)

STAGING_DIRNAME = ".incoming"
STAGING_TTL_S = 6 * 3600

# Прогресс приёма тела: upload_id -> [получено байт, всего байт, отметка].
# Клиент шлёт ?upload_id= (uuid hex) и опрашивает статус, пока его XHR
# ещё летит: бар серверного приёма продолжает бар отправки без разрывов.
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_PROGRESS: dict = {}
_UPLOAD_PROGRESS_LOCK = threading.Lock()


def valid_upload_id(value: object) -> bool:
    """upload_id — это uuid hex клиента (иначе 404, не 500)."""
    return isinstance(value, str) and _UPLOAD_ID_RE.match(value) is not None


def get_upload_progress(upload_id: str):
    """(получено, всего) или None (неизвестно/готово — считать готовым)."""
    with _UPLOAD_PROGRESS_LOCK:
        item = _UPLOAD_PROGRESS.get(upload_id)
        if item is None:
            return None
        return (item[0], item[1])


def _progress_set(upload_id: str, received: int, total: int) -> None:
    with _UPLOAD_PROGRESS_LOCK:
        _UPLOAD_PROGRESS[upload_id] = [received, total, time.time()]


def _progress_drop(upload_id: str) -> None:
    with _UPLOAD_PROGRESS_LOCK:
        _UPLOAD_PROGRESS.pop(upload_id, None)


def _progress_sweep(max_age_s: int = STAGING_TTL_S) -> None:
    now = time.time()
    with _UPLOAD_PROGRESS_LOCK:
        stale = [k for k, v in _UPLOAD_PROGRESS.items()
                 if now - v[2] > max_age_s]
        for k in stale:
            _UPLOAD_PROGRESS.pop(k, None)


def _workspace_root() -> str:
    import workspace as ws_module

    return ws_module.WORKSPACE_ROOT


def _staging_root() -> str:
    root = os.path.join(_workspace_root(), STAGING_DIRNAME)
    os.makedirs(root, exist_ok=True)
    return root


def sweep_staging(max_age_s: int = STAGING_TTL_S) -> int:
    """Удаляет недокачанные аплоады старше max_age_s. Возвращает число."""
    try:
        names = os.listdir(_staging_root())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for name in names:
        full = os.path.join(_staging_root(), name)
        try:
            if now - os.path.getmtime(full) < max_age_s:
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full, ignore_errors=True)
            else:
                os.remove(full)
            removed += 1
        except OSError:
            continue
    return removed


class StagedUploadedFile(UploadedFile):
    """UploadedFile, уже лежащий в стейджинге (staged_path).

    Закрытие НЕ удаляет файл (его забирает rename в размещение);
    мусор чистит sweep_staging.
    """

    def __init__(self, staged_path: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.staged_path = staged_path


class StagingUploadHandler(FileUploadHandler):
    """Пишет чанки сразу в стейджинг на ФС workspace-ов."""

    def __init__(self, request=None) -> None:
        super().__init__(request)
        self._batch = uuid.uuid4().hex
        self._current: Optional[Tuple[str, BinaryIO, dict]] = None
        self._swept = False
        self._upload_id: Optional[str] = None
        self._received = 0
        self._total = 0
        try:
            candidate = (request.GET.get("upload_id")
                         if request is not None else None)
        except Exception:
            candidate = None
        if valid_upload_id(candidate):
            self._upload_id = candidate
            try:
                self._total = int(
                    request.META.get("CONTENT_LENGTH") or 0)
            except (TypeError, ValueError):
                self._total = 0

    def new_file(self, field_name, file_name, content_type,
                 content_length, charset=None, content_type_extra=None):
        super().new_file(field_name, file_name, content_type, content_length,
                         charset, content_type_extra)
        if not self._swept:
            self._swept = True
            try:
                sweep_staging()
                _progress_sweep()
            except Exception:
                pass
        batch_dir = os.path.join(_staging_root(), self._batch)
        os.makedirs(batch_dir, exist_ok=True)
        # Имя поля фиксировано формой (source/preview/file) — безопасно.
        staged = os.path.join(batch_dir, os.path.basename(field_name))
        handle = open(staged, "wb")
        self._current = (staged, handle, {
            "name": file_name,
            "content_type": content_type,
            "charset": charset,
        })
        # Дальше по цепочке никого: temp/memory-хендлеры файл не увидят.
        raise StopFutureHandlers()

    def receive_data_chunk(self, raw_data, start):
        if self._current is None:
            return None
        _, handle, _ = self._current
        handle.write(raw_data)
        if self._upload_id is not None:
            self._received += len(raw_data)
            _progress_set(self._upload_id, self._received, self._total)
        # None — чанк потреблён, цепочка останавливается.
        return None

    def file_complete(self, file_size):
        if self._current is None:
            return None
        staged, handle, info = self._current
        self._current = None
        try:
            handle.close()
        except OSError:
            pass
        handle_ro = open(staged, "rb")
        uploaded = StagedUploadedFile(
            staged,
            handle_ro,
            info["name"],
            info["content_type"],
            file_size,
            info["charset"],
        )
        uploaded.staged_path = staged
        return uploaded

    def upload_interrupted(self):
        # Обрыв соединения: недокачанное — сразу в мусор, вместе
        # с опустевшим batch-каталогом (иначе .incoming зарастает
        # задолго до 6-часового sweep).
        if self._upload_id is not None:
            _progress_drop(self._upload_id)
        if self._current is not None:
            staged, handle, _ = self._current
            self._current = None
            try:
                handle.close()
            except OSError:
                pass
            try:
                os.remove(staged)
            except OSError:
                pass
            try:
                os.rmdir(os.path.dirname(staged))
            except OSError:
                pass
