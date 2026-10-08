# -*- coding: utf-8 -*-
"""Бэкап/восстановление задач и проектов (zip по формату backup_format).

GET  workspaces/<id>/backup  — полный слепок задачи: manifest.json,
                               task.json, fragments.tsv, все видео папки
                               (exports/ и точечные временные файлы не
                               входят).
GET  projects/<pid>/backup   — слепок проекта: manifest.json, project.json
                               и task_i/ с полными слепками задач.
POST backups/import          — восстановление архива (kind=backup) либо
                               создание задачи из архива разметки с видео
                               (kind=annotations).

Сборка — синхронно во временном zip-файле в корне WORKSPACE_ROOT (тот же
диск, где живут задачи; gunicorn gthread с timeout 1800 долгие GET
допускает) и отдаётся FileResponse чанками; закрытие ответа удаляет файл.
Распаковка идёт в скрытый от сканера стейджинг ``.import-*`` → правка
task.json → атомарный ``os.replace`` в целевую папку; перезапись
существующей задачи — только после успешной распаковки.

Файловая семантика — через харнесс videocutter.standalone (workspace_path,
sanitize_workspace_name, write_stream, VIDEO_EXTS); членство —
project_meta/task_meta; формат — backup_format (чистый stdlib-модуль).
Правила архива — docs/backup-model.md.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import threading
import time
import zipfile
import zlib
from typing import Callable, Dict, List, Optional, Tuple

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_http_methods

import backup_format as fmt
import project_meta
import task_meta
import workspace as ws_module
from videocutter.standalone import workspace as ws_fs
from workspace import get_workspace, scan_workspaces
from vc_pairs import frame_provider
from vc_pairs.views import (_storage_error, _validate_workspace_async,
                            _workspace_delete, _ws_404)

HTTP_CONFLICT = 409


class _Problem(Exception):
    """Отказ импорта с готовым HTTP-статусом (текст — ответ клиенту)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ─── Временный архив и отдача файла ─────────────────────────────────────────

def make_temp_zip_path() -> str:
    """Временный файл архива в корне workspace-ов (тот же диск, dot-имя)."""
    root = ws_module.WORKSPACE_ROOT
    os.makedirs(root, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix=".backup-", suffix=".zip", dir=root)
    os.close(fd)
    return path


def unlink_quiet(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


class TempFile:
    """Файловый объект временного архива: закрытие удаляет файл.

    FileResponse регистрирует ``close`` как resource-closer — при любом
    завершении отдачи (успех, обрыв клиента, падение воркера) временный
    zip не остаётся на диске.
    """

    def __init__(self, path: str):
        self._path = path
        self._file = open(path, "rb")

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            unlink_quiet(self._path)

    def __getattr__(self, name):  # seek/tell/fileno/... — прозрачная делегация
        return getattr(self._file, name)


def file_response(path: str, filename: str) -> FileResponse:
    """Отдаёт временный архив чанками (Content-Length — размер файла)."""
    try:
        return FileResponse(TempFile(path), as_attachment=True,
                            filename=filename, content_type="application/zip")
    except BaseException:
        unlink_quiet(path)
        raise


def media_fingerprint(ws) -> Dict[str, object]:
    """Отпечаток медиа задачи для манифеста: без нового декода.

    ``try_get_metadata`` — быстрый путь (in-memory/дисковый кэш); пока индекс
    не построен, поля нулевые — и сверка отпечатка при импорте отключается
    (не роняет бэкап и импорт, см. backup_format.fingerprint_problem).
    """
    path = ws.visualization or ws.original or ""
    meta = (frame_provider.try_get_metadata(path) if path else None) or {}
    return {
        "total_frames": meta.get("total_frames", 0),
        "width": meta.get("width", 0),
        "height": meta.get("height", 0),
        "fps": meta.get("fps", 0.0),
        "roles": {
            "source": os.path.basename(ws.original) if ws.original else None,
            "preview": os.path.basename(ws.visualization) if ws.visualization else None,
        },
    }


def video_members(ws_path: str) -> List[str]:
    """Видеофайлы папки задачи: плоские, без точечных временных файлов."""
    out = []
    for entry in sorted(os.listdir(ws_path)):
        if entry.startswith("."):
            continue
        full = os.path.join(ws_path, entry)
        if os.path.isfile(full) and \
                os.path.splitext(entry)[1].lower() in ws_fs.VIDEO_EXTS:
            out.append(entry)
    return out


def _write_manifest(zf: zipfile.ZipFile, manifest: Dict[str, object]) -> None:
    zf.writestr(fmt.MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2))


def _write_task_members(zf: zipfile.ZipFile, ws_path: str,
                        prefix: str = "") -> None:
    """Кладёт в архив task.json, fragments.tsv и видео папки задачи."""
    wanted = [name for name in (task_meta.TASK_FILE, ws_module.FRAGMENTS_FILE)
              if os.path.isfile(os.path.join(ws_path, name))]
    wanted += video_members(ws_path)
    for name in wanted:
        compress = (zipfile.ZIP_STORED
                    if os.path.splitext(name)[1].lower() in ws_fs.VIDEO_EXTS
                    else zipfile.ZIP_DEFLATED)
        zf.write(os.path.join(ws_path, name), prefix + name,
                 compress_type=compress)


# ─── GET: бэкап задачи и проекта ────────────────────────────────────────────

@require_http_methods(["GET"])
def workspace_backup(request, workspace_id: str):
    """GET — полный бэкап задачи (zip, скачивается потоком)."""
    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)

    path = make_temp_zip_path()
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            manifest = fmt.make_manifest(
                fmt.KIND_BACKUP, resource=fmt.RESOURCE_TASK,
                task_id=workspace_id,
                project_id=project_meta.resolve_pid(
                    task_meta.load(ws.path).get("project_id")),
                media=media_fingerprint(ws))
            _write_manifest(zf, manifest)
            _write_task_members(zf, ws.path)
    except OSError as e:
        unlink_quiet(path)
        return _storage_error(e)
    except BaseException:
        unlink_quiet(path)
        raise
    return file_response(path, f"{workspace_id}-backup.zip")


@require_http_methods(["GET"])
def project_backup(request, project_id: str):
    """GET — бэкап проекта: project.json + полные слепоки задач task_i/."""
    if not project_meta.exists(project_id):
        return JsonResponse(
            {"error": f"Проект '{project_id}' не найден"}, status=404)

    members = sorted(name for name, pid
                     in project_meta.task_project_ids().items()
                     if pid == project_id)

    path = make_temp_zip_path()
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Слепоки задач — первыми: манифест перечисляет реально
            # попавшие в архив папки (задача могла уйти между снапшотом
            # и сборкой).
            entries = []
            for name in members:
                ws_path = os.path.join(ws_module.WORKSPACE_ROOT, name)
                if not os.path.isdir(ws_path):
                    continue  # удалили между снапшотом и сборкой
                dirname = f"task_{len(entries)}"
                _write_task_members(zf, ws_path, prefix=f"{dirname}/")
                entries.append({"dir": dirname, "task_id": name})
            manifest = fmt.make_manifest(
                fmt.KIND_BACKUP, resource=fmt.RESOURCE_PROJECT,
                project_id=project_id, tasks=entries)
            _write_manifest(zf, manifest)
            meta = project_meta.load(project_id) or project_meta.defaults()
            zf.writestr(project_meta.PROJECT_FILE,
                        json.dumps(meta, ensure_ascii=False, indent=2))
    except OSError as e:
        unlink_quiet(path)
        return _storage_error(e)
    except BaseException:
        unlink_quiet(path)
        raise
    return file_response(path, f"{project_id}-backup.zip")


# ─── Стейджинг импорта ──────────────────────────────────────────────────────

#: Стейджинги импорта — точечные папки в корне (сканер их не видит).
_STAGING_PREFIX = ".import-"
#: Осиротевший стейджинг (крэш посреди импорта) чистится через сутки:
#: живой процесс держит путь в _live_staging, чужому воркеру не мешает.
_STALE_STAGING_S = 24 * 3600

_staging_lock = threading.Lock()
_live_staging: set = set()


def _staging_dir() -> str:
    """Создаёт стейджинг в корне workspace-ов (та же ФС — rename дешёвый)."""
    root = ws_module.WORKSPACE_ROOT
    os.makedirs(root, exist_ok=True)
    with _staging_lock:
        path = tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=root)
        _live_staging.add(path)
    _sweep_stale_staging()
    return path


def _drop_staging(path: Optional[str]) -> None:
    """Убирает стейджинг (и забывает путь — после переезда он уже чужой)."""
    if not path:
        return
    with _staging_lock:
        _live_staging.discard(path)
    shutil.rmtree(path, ignore_errors=True)


def _sweep_stale_staging() -> None:
    """Чистит осиротевшие стейджинги (kill/крэш посреди импорта)."""
    root = ws_module.WORKSPACE_ROOT
    if not os.path.isdir(root):
        return
    with _staging_lock:
        live = set(_live_staging)
    now = time.time()
    for entry in os.listdir(root):
        if not entry.startswith(_STAGING_PREFIX):
            continue
        full = os.path.join(root, entry)
        if full in live or not os.path.isdir(full):
            continue
        try:
            if now - os.path.getmtime(full) > _STALE_STAGING_S:
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass  # конкурентное удаление — не наша забота


def _resolve_target(base: str, on_conflict: str,
                    exists: Callable[[str], bool], what: str) -> str:
    """Целевое имя по правилу on_conflict: error → 409, rename → суффиксы."""
    if not exists(base):
        return base
    if on_conflict == "error":
        raise _Problem(HTTP_CONFLICT, f"{what} '{base}' уже существует")
    if on_conflict == "overwrite":
        return base
    i = 1
    while exists(f"{base}_{i}"):
        i += 1
    return f"{base}_{i}"


def _task_exists(name: str) -> bool:
    return os.path.isdir(os.path.join(ws_module.WORKSPACE_ROOT, name))


def _task_members(names: List[str], prefix: str):
    """Члены папки задачи: (видео, task.json|None, fragments.tsv|None).

    Только плоские имена под префиксом: точечные и вложенности (exports/)
    не переносятся — состав папки задаёт бэкап, а не чужой архив.
    Члены чужих префиксов отбрасываются (слепой срез давал коллизии:
    ``task_0/task.json`` под префиксом ``task_1/`` превращался в
    ``task.json`` — задача забирала чужой паспорт и разметку).
    """
    videos: List[str] = []
    passport: Optional[str] = None
    tsv: Optional[str] = None
    for member in names:
        if prefix and not member.startswith(prefix):
            continue
        rel = member[len(prefix):] if prefix else member
        if not rel or rel.endswith("/") or rel.startswith(".") or "/" in rel:
            continue
        if rel == task_meta.TASK_FILE:
            passport = member
        elif rel == ws_module.FRAGMENTS_FILE:
            tsv = member
        elif os.path.splitext(rel)[1].lower() in ws_fs.VIDEO_EXTS:
            videos.append(member)
    return videos, passport, tsv


def _archived_task_meta(zf: zipfile.ZipFile,
                        member: Optional[str]) -> Dict[str, object]:
    """task.json из архива: строго читается и валидируется (не дефолтами)."""
    if member is None:
        return task_meta.defaults()
    try:
        data = fmt.read_json_member(zf, member)
    except fmt.ArchiveError as e:
        raise _Problem(400, str(e))
    try:
        return task_meta.validate(data)
    except ValueError as e:
        raise _Problem(400, f"Некорректный task.json в архиве: {e}")


def _extract(zf: zipfile.ZipFile, staging: str, prefix: str,
             members: List[str]) -> None:
    """Потоково кладёт члены архива в стейджинг (через write_stream харнесса)."""
    for member in members:
        if prefix and not member.startswith(prefix):
            continue  # чужой префикс — см. _task_members
        rel = member[len(prefix):] if prefix else member
        with zf.open(member) as src:
            ws_fs.write_stream(src, os.path.join(staging, rel))


def _stage_task(zf: zipfile.ZipFile, names: List[str],
                manifest: Dict[str, object], *, prefix: str,
                on_conflict: str, forced_name: Optional[str] = None,
                project_override: Optional[str] = None
                ) -> Tuple[str, str, Optional[str]]:
    """Все проверки задачи архива и распаковка в стейджинг.

    Возвращает (целевое имя, путь стейджинга, project_id). В целевую папку
    ничего не пишется: при любом отказе стейджинг вычищается и существующая
    задача остаётся нетронутой (перезапись — только после успешной
    распаковки). Привязка к проекту: override > из архива (только если
    проект есть в реестре) > null.
    """
    videos, passport, tsv = _task_members(names, prefix)
    if not videos:
        if manifest.get("kind") == fmt.KIND_ANNOTATIONS:
            raise _Problem(
                400,
                "В архиве нет видеофайлов. Разметку существующей задачи "
                "импортируйте через «Импорт разметки» в карточке задачи")
        raise _Problem(
            400, "В архиве нет видеофайлов — восстановить задачу не из чего")

    if forced_name is not None:
        target = forced_name
    else:
        raw = str(manifest.get("task_id") or "task")
        try:
            base = ws_fs.sanitize_workspace_name(raw)
        except ws_fs.InvalidWorkspaceError:
            base = "task"
        if base.startswith("."):
            raise _Problem(
                400, f"Имя задачи в архиве начинается с точки: {raw!r}")
        if base == project_meta.PROJECTS_DIRNAME:
            raise _Problem(
                400, "Имя 'projects' зарезервировано под реестр проектов")
        target = _resolve_target(base, on_conflict, _task_exists, "Задача")

    meta = _archived_task_meta(zf, passport)
    pid = project_override
    if pid is None:
        pid = project_meta.resolve_pid(manifest.get("project_id")) \
            or project_meta.resolve_pid(meta.get("project_id"))
    meta["project_id"] = pid

    staging = _staging_dir()
    members = ([passport] if passport else []) \
        + ([tsv] if tsv else []) + videos
    try:
        _extract(zf, staging, prefix, members)
        task_meta.save(staging, meta)
    except _Problem:
        _drop_staging(staging)
        raise
    except OSError:
        _drop_staging(staging)
        raise
    except (zipfile.BadZipFile, zlib.error, EOFError) as e:
        _drop_staging(staging)
        raise _Problem(400, f"Архив повреждён: {e}")
    return target, staging, pid


def _place_task(staging: str, target: str, on_conflict: str) -> None:
    """Атомарно переезжает стейджинг в целевую папку задачи.

    Перезапись — через _workspace_delete (останавливает фон задачи и чистит
    процессные кэши) и только после успешной распаковки.
    """
    target_path = ws_fs.workspace_path(ws_module.WORKSPACE_ROOT, target)
    if on_conflict == "overwrite" and os.path.isdir(target_path):
        resp = _workspace_delete(target)
        if resp.status_code != 200:
            try:
                message = resp.json().get("error") or "не удалось удалить"
            except ValueError:
                message = "не удалось удалить существующую задачу"
            raise _Problem(resp.status_code, message)
    try:
        os.replace(staging, target_path)
    except OSError as e:
        if e.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR):
            raise _Problem(HTTP_CONFLICT, f"Задача '{target}' уже существует")
        raise
    with _staging_lock:
        _live_staging.discard(staging)


def _launch_validation(target: str) -> None:
    """Паспорт/индекс импортированной задачи — в фоне, без автопере­веса.

    ``join_default=False``: членство уже решено при импорте, авто-перевес
    в default молча сломал бы это решение.
    """
    threading.Thread(target=_validate_workspace_async, args=(target, False),
                     daemon=True).start()


# ─── POST: импорт ───────────────────────────────────────────────────────────

def _import_task(zf: zipfile.ZipFile, names: List[str],
                 manifest: Dict[str, object], on_conflict: str,
                 project_override: Optional[str]) -> JsonResponse:
    target, staging, pid = _stage_task(
        zf, names, manifest, prefix="", on_conflict=on_conflict,
        project_override=project_override)
    try:
        _place_task(staging, target, on_conflict)
    except BaseException:
        _drop_staging(staging)
        raise
    scan_workspaces()
    _launch_validation(target)
    return JsonResponse(
        {"kind": manifest.get("kind"), "id": target, "project_id": pid},
        status=202)


def _project_entries(manifest: Dict[str, object],
                     names: List[str]) -> List[Dict[str, str]]:
    """Задачи бэкапа проекта: список из манифеста (в т.ч. пустой = пустой
    проект), иначе — производные по префиксам task_*/ членов архива."""
    raw = manifest.get("tasks")
    if isinstance(raw, list):
        entries = []
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("dir"), str):
                dirname = item["dir"]
                if not dirname or dirname.startswith((".", "/")) \
                        or "/" in dirname or "\\" in dirname:
                    raise _Problem(400, f"Некорректный префикс задачи: {dirname!r}")
                entries.append({
                    "dir": dirname,
                    "task_id": str(item.get("task_id") or dirname),
                })
        return entries

    dirs = sorted({n.split("/", 1)[0] for n in names
                   if "/" in n and not n.split("/", 1)[0].startswith(".")})
    if not dirs:
        raise _Problem(400, "В архиве не найдено задач проекта")
    return [{"dir": d, "task_id": d} for d in dirs]


def _write_project_json(zf: zipfile.ZipFile, names: List[str], pid: str,
                        on_conflict: str) -> None:
    """Создаёт/обновляет project.json: поля архива поверх базы, id — резолвнутый."""
    archived: Dict[str, object] = {}
    if "project.json" in names:
        try:
            archived = fmt.read_json_member(zf, "project.json")
        except fmt.ArchiveError as e:
            raise _Problem(400, str(e))
    if on_conflict == "overwrite" and project_meta.exists(pid):
        base_meta = project_meta.load(pid) or project_meta.defaults()
    else:
        base_meta = project_meta.defaults()
    meta = dict(base_meta)
    meta.update(archived)
    meta["id"] = pid
    if not str(meta.get("name") or "").strip():
        meta["name"] = pid
    if meta.get("created_at") is None:
        meta["created_at"] = project_meta.now_iso()
    try:
        project_meta.save(pid, meta)
    except ValueError as e:
        raise _Problem(400, f"Некорректный project.json в архиве: {e}")


def _import_project(zf: zipfile.ZipFile, names: List[str],
                    manifest: Dict[str, object],
                    on_conflict: str) -> JsonResponse:
    """Восстанавливает проект: контейнер, затем задачи task_i/ (все — в стейджинг
    до создания проекта, чтобы отказ не оставил полуконтейнер)."""
    flat_videos = [n for n in names
                   if not n.endswith("/") and "/" not in n
                   and os.path.splitext(n)[1].lower() in ws_fs.VIDEO_EXTS]
    if flat_videos:
        raise _Problem(
            400, "Видео лежит в корне архива — это бэкап задачи, а не проекта")

    raw = str(manifest.get("project_id") or "project")
    try:
        base = ws_fs.sanitize_workspace_name(raw)
    except ws_fs.InvalidWorkspaceError:
        base = "project"
    pid = _resolve_target(base, on_conflict, project_meta.exists, "Проект")

    # Имена задач резолвим ДО создания проекта: конфликт в error-режиме
    # не должен оставить пустой контейнер.
    plan: List[Tuple[Dict[str, str], str, str, List[str],
                     Optional[str], Optional[str]]] = []
    for entry in _project_entries(manifest, names):
        raw_name = str(entry.get("task_id") or "task")
        try:
            base_name = ws_fs.sanitize_workspace_name(raw_name)
        except ws_fs.InvalidWorkspaceError:
            base_name = "task"
        if base_name.startswith("."):
            raise _Problem(
                400, f"Имя задачи в архиве начинается с точки: {raw_name!r}")
        if base_name == project_meta.PROJECTS_DIRNAME:
            raise _Problem(
                400, "Имя 'projects' зарезервировано под реестр проектов")
        target = _resolve_target(base_name, on_conflict, _task_exists, "Задача")
        prefix = entry["dir"] + "/"
        videos, passport, tsv = _task_members(names, prefix)
        if not videos:
            raise _Problem(
                400, f"В бэкапе проекта нет видео ('{entry['dir']}')")
        plan.append((entry, target, prefix, videos, passport, tsv))

    # Стейджинг всех задач: любой отказ вычищает всё, проект не создан.
    staged: List[Tuple[str, str]] = []  # (target, staging)
    try:
        for entry, target, prefix, videos, passport, tsv in plan:
            meta = _archived_task_meta(zf, passport)
            meta["project_id"] = pid
            staging = _staging_dir()
            staged.append((target, staging))
            members = ([passport] if passport else []) \
                + ([tsv] if tsv else []) + videos
            _extract(zf, staging, prefix, members)
            task_meta.save(staging, meta)
        _write_project_json(zf, names, pid, on_conflict)
    except BaseException:
        for _, staging in staged:
            _drop_staging(staging)
        raise

    try:
        for target, staging in staged:
            _place_task(staging, target, on_conflict)
    except BaseException:
        # Уже переехавшие задачи валидны и остаются (частичный импорт —
        # причина в ответе); чистим только не размещённые стейджинги.
        for _, staging in staged:
            _drop_staging(staging)
        raise

    scan_workspaces()
    for target, _ in staged:
        _launch_validation(target)
    return JsonResponse(
        {"kind": manifest.get("kind"), "id": pid,
         "tasks": [target for target, _ in staged]},
        status=202)


@require_http_methods(["POST"])
def backup_import(request):
    """POST multipart ``file`` — восстановление архива (ответ 202).

    ``?on_conflict=error|rename|overwrite`` (дефолт rename) — что делать при
    совпадении имён; ``?project=<pid>`` — перепривязка импортируемой задачи
    (только для архива задачи; у бэкапа проекта членство — из архива).
    Ответ 202: файлы записаны, индексация — в фоне (опрос списка/detail).
    """
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse(
            {"error": "Не передан файл архива (поле 'file')"}, status=400)
    on_conflict = request.GET.get("on_conflict", "rename")
    if on_conflict not in ("error", "rename", "overwrite"):
        return JsonResponse(
            {"error": "on_conflict: ожидается error | rename | overwrite"},
            status=400)
    project_override = request.GET.get("project")
    if project_override is not None and not project_meta.exists(project_override):
        return JsonResponse(
            {"error": f"Проект '{project_override}' не найден"}, status=400)

    try:
        zf, names, manifest = fmt.open_archive(upload)
    except fmt.ArchiveError as e:
        return JsonResponse({"error": str(e)}, status=400)

    try:
        if manifest.get("kind") == fmt.KIND_BACKUP and (
                manifest.get("resource") == fmt.RESOURCE_PROJECT
                or "project.json" in names):
            if project_override is not None:
                return JsonResponse(
                    {"error": "?project не применяется к бэкапу проекта: "
                              "членство берётся из архива"},
                    status=400)
            return _import_project(zf, names, manifest, on_conflict)
        return _import_task(zf, names, manifest, on_conflict,
                            project_override)
    except _Problem as e:
        return JsonResponse({"error": e.message}, status=e.status)
    except OSError as e:
        return _storage_error(e)
    finally:
        zf.close()
