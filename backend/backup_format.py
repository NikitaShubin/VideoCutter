# -*- coding: utf-8 -*-
"""Архивы бэкапа и обмена разметкой: манифест, безопасность, отпечаток.

Формат — zip с ``manifest.json`` в корне; два вида (kind):

* ``backup``      — полный слепок задачи (``resource=task``) или проекта
                    (``resource=project``, задачи в папках ``task_i/``);
* ``annotations`` — разметка ``fragments.tsv`` + отпечаток медиа; видео —
                    только по явному запросу (``?media=1``).

Модуль чистый: stdlib (json/zipfile) + константы формата, без Django и без
обращений к ФС — правила проверяются юнит-тестами отдельно от контура
(локального или VDO). Файловая семантика (папки задач, роли видео, паспорта)
живёт в харнессе ``videocutter.standalone`` и модулях ``task_meta`` /
``project_meta``; здесь — только версии, безопасность имён членов архива
и сверка отпечатка. Правила и примеры — docs/backup-model.md.
"""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

#: Идентификатор формата (значение поля ``format`` манифеста).
FORMAT_NAME = "videocutter-backup"

#: Версия формата. Читатель пропускает младшие версии и старше своей —
#: только по major: 1.x читается всегда, 2.0 — уже отказ.
FORMAT_VERSION = "1.0"
FORMAT_MAJOR = 1

#: Имя манифеста в корне каждого архива.
MANIFEST_NAME = "manifest.json"

#: Имя файла разметки внутри задачи (сверяется тестом с workspace.FRAGMENTS_FILE).
FRAGMENTS_FILE = "fragments.tsv"

KIND_BACKUP = "backup"
KIND_ANNOTATIONS = "annotations"
KINDS = (KIND_BACKUP, KIND_ANNOTATIONS)

RESOURCE_TASK = "task"
RESOURCE_PROJECT = "project"

#: Потолок размера манифеста: он читается целиком в память (анти zip-бомба).
MANIFEST_MAX_BYTES = 4 * 1024 * 1024

#: Потолок разметки при импорте (целиком в память для проверки до замены).
FRAGMENTS_MAX_BYTES = 64 * 1024 * 1024

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ArchiveError(ValueError):
    """Архив отвергнут на входе: текст — готовая причина для HTTP-ответа."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Манифест ────────────────────────────────────────────────────────────────

def make_manifest(kind: str, *, resource: Optional[str] = None,
                  **extra: Any) -> Dict[str, Any]:
    """Собирает манифест архива. Служебные поля перетереть нельзя."""
    if kind not in KINDS:
        raise ValueError(f"Неизвестный kind архива: {kind!r}")
    manifest: Dict[str, Any] = dict(extra)
    manifest.update({
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "kind": kind,
        "resource": resource,
        "created_at": now_iso(),
    })
    return manifest


def manifest_problem(manifest: Any) -> Optional[str]:
    """Причина, по которой манифест не принимается, или None.

    Неизвестные поля игнорируются (forward-compat: младший читатель
    переживает добавление полей старшей версией формата).
    """
    if not isinstance(manifest, dict):
        return "manifest.json должен быть JSON-объектом"
    if manifest.get("format") != FORMAT_NAME:
        return "не архив VideoCutter (manifest.format не совпадает)"
    raw_version = str(manifest.get("format_version", ""))
    major = raw_version.split(".", 1)[0]
    if not major.isdigit():
        return f"некорректная версия формата: {raw_version!r}"
    if int(major) > FORMAT_MAJOR:
        return (f"архив версии {raw_version} этой версией программы не читается "
                f"(поддерживается {FORMAT_VERSION}.x) — обновите VideoCutter")
    if manifest.get("kind") not in KINDS:
        return f"неизвестный kind архива: {manifest.get('kind')!r}"
    resource = manifest.get("resource")
    if resource is not None and resource not in (RESOURCE_TASK, RESOURCE_PROJECT):
        return f"неизвестный resource архива: {resource!r}"
    for key in ("task_id", "project_id"):
        value = manifest.get(key)
        if value is not None and not isinstance(value, str):
            return f"некорректное поле {key} манифеста"
    return None


# ─── Безопасность имён членов архива ─────────────────────────────────────────

def member_problem(name: Any) -> Optional[str]:
    """Причина небезопасности имени члена архива или None.

    Блокируется всё, что может выйти за целевую папку при распаковке:
    абсолютные пути, ``..``-переходы, обратные слэши (чужой разделитель),
    control-символы. Директории (имя с ``/`` на конце) допускаются —
    распаковка их просто пропускает.
    """
    if not isinstance(name, str) or not name:
        return "пустое имя файла в архиве"
    if "\x00" in name or any(ord(ch) < 32 for ch in name):
        return f"недопустимые символы в имени файла: {name!r}"
    if "\\" in name:
        return f"небезопасное имя файла (обратный слэш): {name!r}"
    if name.startswith("/") or _DRIVE_RE.match(name):
        return f"абсолютный путь в архиве: {name!r}"
    stripped = name.rstrip("/") or "/"
    norm = posixpath.normpath(stripped)
    if norm.startswith("/") or norm == ".." or norm.startswith("../"):
        return f"выход за пределы архива: {name!r}"
    if norm == "." and stripped != ".":
        return f"некорректное имя файла: {name!r}"
    return None


def safe_members(zf: zipfile.ZipFile) -> Tuple[List[str], Optional[str]]:
    """Имена членов и первая найденная проблема (или None)."""
    names = zf.namelist()
    for name in names:
        problem = member_problem(name)
        if problem is not None:
            return names, problem
    return names, None


def open_archive(fileobj: Any) -> Tuple[zipfile.ZipFile, List[str], Dict[str, Any]]:
    """Открывает архив и проверяет его целиком: имена членов + манифест.

    Возвращает ``(zf, names, manifest)``; вызывающий обязан ``zf.close()``
    (в ``finally``). Любая проблема — :class:`ArchiveError` с текстом
    для ответа.
    """
    try:
        zf = zipfile.ZipFile(fileobj)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
        raise ArchiveError(f"это не zip-архив: {e}")

    names, problem = safe_members(zf)
    if problem is not None:
        zf.close()
        raise ArchiveError(f"Небезопасное содержимое архива: {problem}")

    try:
        info = zf.getinfo(MANIFEST_NAME)
    except KeyError:
        zf.close()
        raise ArchiveError(f"в архиве нет {MANIFEST_NAME} — это не архив VideoCutter")
    if info.file_size > MANIFEST_MAX_BYTES:
        zf.close()
        raise ArchiveError(f"{MANIFEST_NAME} подозрительно большой")
    try:
        raw = zf.read(MANIFEST_NAME)
        manifest = json.loads(raw.decode("utf-8"))
    except (zipfile.BadZipFile, RuntimeError, UnicodeDecodeError, ValueError) as e:
        zf.close()
        raise ArchiveError(f"{MANIFEST_NAME} не читается: {e}")

    problem = manifest_problem(manifest)
    if problem is not None:
        zf.close()
        raise ArchiveError(problem)
    return zf, names, manifest


def read_json_member(zf: zipfile.ZipFile, member: str,
                     max_bytes: int = MANIFEST_MAX_BYTES) -> Dict[str, Any]:
    """Читает JSON-член архива (dict) с потолком размера.

    :raises ArchiveError: нет члена, битый JSON, не объект, слишком большой.
    """
    try:
        info = zf.getinfo(member)
    except KeyError:
        raise ArchiveError(f"в архиве нет {member}")
    if info.file_size > max_bytes:
        raise ArchiveError(f"{member} подозрительно большой")
    try:
        data = json.loads(zf.read(member).decode("utf-8"))
    except (zipfile.BadZipFile, RuntimeError, UnicodeDecodeError, ValueError) as e:
        raise ArchiveError(f"{member} не читается: {e}")
    if not isinstance(data, dict):
        raise ArchiveError(f"{member} должен быть JSON-объектом")
    return data


# ─── Отпечаток медиа ─────────────────────────────────────────────────────────

def fingerprint_problem(want: Any, got: Dict[str, Any]) -> Optional[str]:
    """Расхождение отпечатка медиа архива с текущей задачей или None.

    Сверяется число видимых кадров — по нему же индексируются фрагменты;
    нулевые значения (индекс не готов / отсутствует в архиве) сверку
    отключают, а не роняют импорт.
    """
    if not isinstance(want, dict):
        return None
    try:
        want_frames = int(want.get("total_frames") or 0)
        got_frames = int(got.get("total_frames") or 0)
    except (TypeError, ValueError):
        return None
    if want_frames > 0 and got_frames > 0 and want_frames != got_frames:
        return (f"Разметка от другого видео: в архиве {want_frames} кадров, "
                f"в задаче {got_frames}")
    return None
