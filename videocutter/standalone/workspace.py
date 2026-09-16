# -*- coding: utf-8 -*-
"""Жизненный цикл workspace на уровне файловой системы (обвязка).

Workspace автономного режима — это папка с видеофайлами и ``fragments.tsv``.
Ядро (``videocutter.core``) о таком устройстве не знает: создание/удаление
папок и ролей — задача обвязки. Эти функции не привязаны к Django или
какому-либо контуру — их вызывает любой внешний слой.

Роли видео (как в десктопном PyVideoCutter: ``preview`` — что показывает не
пользователя, ``source`` — из чего вырезаются фрагменты) кодируются **в имени
файла**: ``<base>_source.<ext>`` / ``<base>_preview.<ext>``. Так можно хранить
два видео с одинаковыми оригинальными именами (они различаются суффиксом), а
смена ролей — это переименование файлов. Одиночный файл без маркера роли —
«нейтральный»: он играет обе роли сразу (source = preview).
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Optional, Sequence, Tuple

# Расширения видеофайлов, которые распознаёт автономная обвязка при сканировании.
# Длина списка принципиальна: файлы с неизвестным расширением не будут
# обнаружены как видео (это ограничение файлового сканера, а не ядра).
VIDEO_EXTS = {
    ".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm",
    ".mts", ".m2ts", ".ts", ".flv", ".wmv",
    ".mpg", ".mpeg", ".3gp", ".3g2", ".ogv", ".ogm", ".asf", ".vob",
}

# Имя workspace — одна папка: без разделителей пути и control-символов.
_NAME_RE = re.compile(r"^[^/\\\x00-\x1f]+$")

# Роли видео и их маркеры в имени файла.
ROLES = ("source", "preview")
_EXACT_ROLE_SUFFIXES = {"_source": "source", "_preview": "preview"}
_LEGACY_PREVIEW_SUFFIXES = ("_viz", "_visualization", "_vis")

# Не-видео расширение для временных файлов загрузки/свапа (сканер их игнорирует).
_TMP_SUFFIX = ".part"

_CHUNK = 1024 * 1024


class InvalidWorkspaceError(ValueError):
    """Некорректное имя/файл/операция workspace."""


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


def _ext_ok(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTS


def _exact_role(filename: str) -> Optional[str]:
    """Точный маркер роли в имени файла: 'source' | 'preview' | None."""
    stem = os.path.splitext(filename)[0].lower()
    return _EXACT_ROLE_SUFFIXES.get(
        next((s for s in _EXACT_ROLE_SUFFIXES if stem.endswith(s)), ""))


def role_of_name(filename: str) -> Optional[str]:
    """Точная роль, закодированная в имени файла ('source'|'preview'), или None."""
    return _exact_role(filename)

def _is_legacy_viz(filename: str) -> bool:
    """Файл со старым маркером превью (_viz/_visualization/_vis)."""
    stem = os.path.splitext(filename)[0].lower()
    return stem.endswith(_LEGACY_PREVIEW_SUFFIXES)


def role_filename(filename: str, role: str) -> str:
    """Имя файла с маркером роли: убирает любой маркер и добавляет целевой.

    ``role_filename("video.mp4", "source")`` -> ``video_source.mp4``;
    ``role_filename("video_source.mp4", "source")`` -> ``video_source.mp4``
    (повторное назначение той же роли — no-op).
    """
    if role not in ROLES:
        raise InvalidWorkspaceError(f"Неизвестная роль: {role!r}")
    ext = os.path.splitext(filename)[1]
    stem = os.path.splitext(filename)[0]
    lower = stem.lower()
    for marker in (*_EXACT_ROLE_SUFFIXES, *_LEGACY_PREVIEW_SUFFIXES):
        if lower.endswith(marker):
            stem = stem[: -len(marker)]
            break
    return stem + f"_{role}" + ext


def classify_videos(root: str, name: str) -> dict:
    """Разделяет видеофайлы workspace на source/preview/unassigned.

    Приоритет:
      1. Точные маркеры ролей ``_source``/``_preview``;
      2. legacy-маркеры превью (``_viz`` и др.) — только если превью не задано
         маркером (обратная совместимость со старыми flat-workspaces);
      3. одиночный «нейтральный» файл без маркера играет обе роли;
      4. пара нейтральных файлов: первый — source-кандидат (и обе роли ему),
         остальные нейтральные — в ``unassigned``.

    Возвращает ``{"source": path|None, "preview": path|None, "unassigned": [..]}``
    """
    d = workspace_path(root, name)
    vfiles = sorted(
        os.path.join(d, f) for f in os.listdir(d)
        if os.path.isfile(os.path.join(d, f)) and _ext_ok(f)
    )

    exact_source = exact_preview = None
    viz: list = []
    plain: list = []
    for p in vfiles:
        base = os.path.basename(p)
        r = _exact_role(base)
        if r == "source":
            exact_source = exact_source or p
        elif r == "preview":
            exact_preview = exact_preview or p
        elif _is_legacy_viz(base):
            viz.append(p)
        else:
            plain.append(p)

    source, preview = exact_source, exact_preview

    # Превью из legacy-маркера (если ещё не назначено точно).
    if preview is None and viz:
        preview = viz[0]

    # Источник: предпочитаем нейтральный файл; иначе — само превью.
    if source is None:
        source = (plain[0] if plain else None) or preview

    # Превью, если единственная роль — только источник (или наоборот).
    if preview is None:
        preview = source

    # Совсем пусто (нет точных маркеров и нет нейтральных после фолбэков).
    if source is None and vfiles:
        source = preview = vfiles[0]

    used = {source, preview}
    unassigned = [
        p for p in vfiles
        if p not in used and not _exact_role(os.path.basename(p))
    ]
    return {"source": source, "preview": preview, "unassigned": unassigned}


def _write_stream(stream: BinaryIO, path: str) -> None:
    with open(path, "wb") as f:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            f.write(chunk)


def create_workspace_pair(
    root: str,
    name: str,
    source: Optional[Tuple[BinaryIO, str]] = None,
    preview: Optional[Tuple[BinaryIO, str]] = None,
) -> str:
    """Создаёт workspace с парой видео (source/preview).

    ``source``/``preview`` — кортежи ``(файлоподобный поток, имя файла)``.
    Оба необязательны, но хотя бы одно должно быть задано. Один файл
    сохраняется «нейтрально» (без маркера роли); пара — с маркерами ролей,
    поэтому одинаковые имена файлов не конфликтуют.

    При ошибке записи созданная папка удаляется (без частичных workspace-ов).
    Возвращает путь к созданной папке.
    """
    if source is None and preview is None:
        raise InvalidWorkspaceError("Нужно передать хотя бы одно видео (source/preview)")

    safe = sanitize_workspace_name(
        name or default_workspace_name(source[1] if source else preview[1])
    )
    dest_dir = workspace_path(root, safe)
    if os.path.exists(dest_dir):
        raise WorkspaceExistsError(f"Workspace '{safe}' уже существует")

    os.makedirs(dest_dir)
    try:
        if source is not None and preview is not None:
            _store_role(dest_dir, "source", *source)
            _store_role(dest_dir, "preview", *preview)
        else:
            stream, filename = source if source is not None else preview
            _write_stream(stream, os.path.join(dest_dir, os.path.basename(filename)))
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return dest_dir


def create_workspace(
    root: str,
    name: str,
    stream: BinaryIO,
    filename: str,
) -> str:
    """Обёртка для обратной совместимости: один файл — «нейтральный» source."""
    return create_workspace_pair(root, name, source=(stream, filename))


def _store_role(dest_dir: str, role: str, stream: BinaryIO, filename: str) -> str:
    target = os.path.join(dest_dir, role_filename(os.path.basename(filename), role))
    _write_stream(stream, target)
    return target


def _found_role_path(d: str, role: str) -> Optional[str]:
    for f in sorted(os.listdir(d)):
        full = os.path.join(d, f)
        if os.path.isfile(full) and _ext_ok(f) and _exact_role(f) == role:
            return full
    return None


def make_upload_temp(root: str, name: str) -> str:
    """Путь временного файла в workspace (для загрузки до валидации)."""
    d = workspace_path(root, name)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f".upload-{os.getpid()}-{uuid.uuid4().hex[:10]}{_TMP_SUFFIX}")


def write_stream(stream: BinaryIO, path: str) -> None:
    """Потоково пишет данные в файл."""
    _write_stream(stream, path)


def promote_plain_video(root: str, name: str, role: str) -> Optional[str]:
    """Даёт роль ранее «нейтральному» (неразмеченному) видеофайлу.

    Переименовывает первый нейтральный (без маркера роли и без legacy-маркера)
    видеофайл в ``<base>_<role>.<ext>``. Возвращает новый путь или None,
    если нейтрального видео нет.
    """
    if role not in ROLES:
        raise InvalidWorkspaceError(f"Неизвестная роль: {role!r}")
    d = workspace_path(root, name)
    for f in sorted(os.listdir(d)):
        full = os.path.join(d, f)
        if not os.path.isfile(full) or not _ext_ok(f):
            continue
        if _exact_role(f) is not None or _is_legacy_viz(f):
            continue
        final = os.path.join(d, role_filename(f, role))
        os.replace(full, final)
        return final
    return None


def commit_role_upload(
    root: str,
    name: str,
    role: str,
    tmp_path: str,
    original_filename: str,
) -> str:
    """Атомарно вносит загруженный (уже провалидированный) файл под роль.

    Перемещает ``tmp_path`` в ``<base>_<role>.<ext>`` и удаляет прежний файл
    этой роли (если его имя отличается от нового).
    """
    if role not in ROLES:
        raise InvalidWorkspaceError(f"Неизвестная роль: {role!r}")
    d = workspace_path(root, name)
    old = _found_role_path(d, role)
    final = os.path.join(d, role_filename(os.path.basename(original_filename), role))
    os.replace(tmp_path, final)
    if old is not None and old != final and os.path.isfile(old):
        os.remove(old)
    return final


def remove_role_file(root: str, name: str, role: str) -> Optional[str]:
    """Удаляет файл роли (с маркером в имени). Возвращает удалённый путь/None."""
    d = workspace_path(root, name)
    path = _found_role_path(d, role)
    if path is not None:
        os.remove(path)
    return path


def assign_role_file(root: str, name: str, role: str, filename: str) -> str:
    """Назначает роль существующему «неразмеченному» видеофайлу.

    ``filename`` — имя нейтрального/unassigned видеофайла (без маркера роли).
    Переименовывает его в ``<base>_<role>.<ext>``, возвращает новый путь.
    Роль нельзя назначить, если этот файл уже размечен, либо она уже занята
    другим файлом.
    """
    if role not in ROLES:
        raise InvalidWorkspaceError(f"Неизвестная роль: {role!r}")
    d = workspace_path(root, name)
    if os.path.basename(filename) != filename:
        raise InvalidWorkspaceError(f"Некорректное имя файла: {filename!r}")
    src = os.path.join(d, filename)
    if not os.path.isfile(src) or not _ext_ok(filename):
        raise InvalidWorkspaceError(f"Видеофайл {filename!r} не найден")
    if _exact_role(filename) is not None or _is_legacy_viz(filename):
        raise InvalidWorkspaceError(f"Файл {filename!r} уже размечен ролью")

    old = _found_role_path(d, role)
    if old is not None and old != src:
        raise InvalidWorkspaceError(
            f"Роль '{role}' уже занята файлом {os.path.basename(old)!r}"
        )

    final = os.path.join(d, role_filename(filename, role))
    os.replace(src, final)
    return final


def swap_role_files(root: str, name: str) -> Tuple[str, str]:
    """Меняет роли двух видео местами (переименование файлов).

    Требует двух разных файлов с точными маркерами ролей. Возвращает
    (новый путь source, новый путь preview).
    """
    d = workspace_path(root, name)
    s = _found_role_path(d, "source")
    p = _found_role_path(d, "preview")
    if not (s and p and s != p):
        raise InvalidWorkspaceError(
            "Нужны два ролевых файла (source и preview), чтобы поменять местами"
        )

    new_preview = os.path.join(d, role_filename(os.path.basename(s), "preview"))
    new_source = os.path.join(d, role_filename(os.path.basename(p), "source"))

    # Атомарный обмен содержимым двух ролевых позиций через временное имя.
    tmp = os.path.join(d, f".swap-{os.getpid()}-{uuid.uuid4().hex[:10]}{_TMP_SUFFIX}")
    os.replace(p, tmp)
    os.replace(s, new_preview)
    os.replace(tmp, new_source)
    return new_source, new_preview


def delete_workspace(root: str, name: str) -> None:
    """Безвозвратно удаляет папку workspace (видео, фрагменты, экспорт)."""
    path = workspace_path(root, name)
    if not os.path.isdir(path):
        raise InvalidWorkspaceError(f"Workspace '{name}' не найден")
    shutil.rmtree(path)