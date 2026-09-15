# -*- coding: utf-8 -*-
"""Жизненный цикл workspace на уровне файловой системы (обвязка).

Workspace автономного режима — это папка с видеофайлом и (опционально)
``fragments.tsv``. Ядро (``videocutter.core``) о таком устройстве не знает:
создание/удаление папок — задача обвязки. Эти функции не привязаны к Django
или какому-либо контуру — их вызывает любой внешний слой.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import BinaryIO

# Расширения видеофайлов, которые распознаёт автономная обвязка.
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".mts"}

# Имя workspace — одна папка: без разделителей пути и control-символов.
_NAME_RE = re.compile(r"^[^/\\\x00-\x1f]+$")

_CHUNK = 1024 * 1024


class InvalidWorkspaceError(ValueError):
    """Некорректное имя/файл workspace."""


class WorkspaceExistsError(ValueError):
    """Workspace с таким именем уже существует."""


def default_workspace_name(filename: str) -> str:
    """Имя workspace по имени файла (без расширения)."""
    return Path(filename).stem


def sanitize_workspace_name(name: str) -> str:
    """Проверяет и нормализует имя workspace (ровно одна папка, без путей)."""
    name = (name or "").strip()
    if name in ("", ".", "..") or not _NAME_RE.match(name):
        raise InvalidWorkspaceError(f"Некорректное имя workspace: {name!r}")
    return name


def workspace_path(root: str, name: str) -> str:
    """Абсолютный путь папки workspace с проверкой, что он внутри ``root``."""
    safe = sanitize_workspace_name(name)
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, safe))
    if os.path.dirname(full) != base:  # защита от выхода за пределы root
        raise InvalidWorkspaceError(f"Путь вне workspace root: {name!r}")
    return full


def create_workspace(
    root: str,
    name: str,
    stream: BinaryIO,
    filename: str,
) -> str:
    """Создаёт workspace и потоково сохраняет в него видеофайл.

    ``stream`` — любой файлоподобный объект с ``read(size)`` (например,
    Django ``UploadedFile``). Возвращает путь к созданной папке.

    При ошибке записи созданная папка удаляется (без частичных workspace-ов).
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in VIDEO_EXTS:
        raise InvalidWorkspaceError(f"Неподдерживаемый формат видео: {filename}")

    safe = sanitize_workspace_name(name or default_workspace_name(filename))
    dest_dir = workspace_path(root, safe)
    if os.path.exists(dest_dir):
        raise WorkspaceExistsError(f"Workspace '{safe}' уже существует")

    dest = os.path.join(dest_dir, os.path.basename(filename))
    try:
        os.makedirs(dest_dir)
        with open(dest, "wb") as f:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return dest_dir


def delete_workspace(root: str, name: str) -> None:
    """Безвозвратно удаляет папку workspace (видео, фрагменты, экспорт)."""
    path = workspace_path(root, name)
    if not os.path.isdir(path):
        raise InvalidWorkspaceError(f"Workspace '{name}' не найден")
    shutil.rmtree(path)
