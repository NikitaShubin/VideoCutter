# -*- coding: utf-8 -*-
"""HTTP API фоновых операций.

Тонкий слой: разбирает запрос, проверяет видимость и отдаёт результат
планировщика. Никакой логики выполнения здесь нет — она в типах операций
и планировщике.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_http_methods

import vc_jobs
import workspace

from .model import DONE, STATUSES, parse_iso
from .scheduler import JobCycleError, JobConflict, JobSpec

logger = logging.getLogger(__name__)


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _owner(request) -> Tuple[str, Tuple[str, ...], bool]:
    """Кто действует: (owner, roles, restricted).

    Заголовки принимаются только от доверенного прокси. Автономно все
    операции принадлежат ``local`` и видны всем.
    """
    if os.getenv("VC_TRUST_PROXY") != "1":
        return "local", (), False
    raw_user = request.META.get("HTTP_X_VC_USER", "").strip()
    roles = tuple(
        role.strip() for role in
        request.META.get("HTTP_X_VC_ROLES", "").split(",") if role.strip())
    if not raw_user:
        # Прокси не передал пользователя — не различаем, как локальный.
        return "local", roles, False
    return raw_user, roles, "admin" not in roles


def _visible(job, identity) -> bool:
    owner, _roles, restricted = identity
    return not restricted or job.owner == owner


def _target_with_project(target: Dict[str, Any]) -> Dict[str, Any]:
    """Дополняет цель-задачу project_id с диска.

    Журнал и фильтр ``project_id`` должны работать и для операций по
    задаче. Указатель живёт в ``task.json``; значение клиента не источник
    правды и перекрывается. Задачи нет в реестре — цель не трогаем.
    """
    if target.get("kind") != "task":
        return target
    task_id = target.get("id")
    if not isinstance(task_id, str) or not task_id:
        return target
    import project_meta

    membership = project_meta.task_project_ids()
    if task_id not in membership:
        return target
    project_id = membership[task_id]
    if target.get("project_id") == project_id:
        return target
    return {**target, "project_id": project_id}


def _job_or_404(scheduler, job_id: str, identity):
    job = scheduler.get(job_id)
    if job is None or not _visible(job, identity):
        return None
    return job


def _serialize(job, scheduler=None) -> Dict[str, Any]:
    data = {**job.to_dict(), "display_status": job.display_status}
    prerequisites = []
    if scheduler is not None:
        for dep_id in job.depends_on:
            dep = scheduler.get(dep_id)
            if dep is None:
                continue
            dep_type = scheduler.registry.get(dep.type)
            prerequisites.append({
                "id": dep.id,
                "type": dep.type,
                "label": dep_type.label if dep_type is not None else dep.type,
                "status": dep.status,
                "display_status": dep.display_status,
            })
    data["prerequisites"] = prerequisites
    data["blocked_by"] = [
        item["id"] for item in prerequisites if item["status"] != DONE]
    return data


def _body(request) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@require_http_methods(["GET", "POST"])
def jobs_collection(request):
    scheduler = vc_jobs.get_scheduler()
    identity = _owner(request)
    if request.method == "GET":
        try:
            limit = int(request.GET.get("limit", "100"))
        except ValueError:
            return _error("limit должен быть числом")
        try:
            jobs, cursor = scheduler.list(
                type=request.GET.get("type") or None,
                status=request.GET.get("status") or None,
                owner=(request.GET.get("owner") or None),
                target=request.GET.get("target") or None,
                project_id=request.GET.get("project_id") or None,
                active=request.GET.get("active") in ("1", "true", "yes"),
                limit=limit,
                cursor=request.GET.get("cursor") or None,
            )
        except ValueError as exc:
            return _error(str(exc))
        visible = [j for j in jobs if _visible(j, identity)]
        return JsonResponse({
            "jobs": [_serialize(j, scheduler) for j in visible],
            "next": cursor,
        })

    data = _body(request)
    if data is None:
        return _error("тело запроса должно быть JSON-объектом")
    job_type = data.get("type")
    target = data.get("target")
    if not isinstance(job_type, str) or not job_type:
        return _error("type обязателен")
    if not isinstance(target, dict):
        return _error("target должен быть объектом")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        return _error("params должен быть объектом")
    priority = data.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        return _error("priority должен быть числом")
    depends_on = data.get("depends_on") or []
    if not isinstance(depends_on, list) \
            or not all(isinstance(x, str) for x in depends_on):
        return _error("depends_on должен быть списком id")
    idempotency_key = data.get("idempotency_key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return _error("idempotency_key должен быть строкой")

    owner, _roles, restricted = identity
    target = _target_with_project(target)
    upload_ids = []
    if target.get("kind") == "upload" and isinstance(target.get("id"), str):
        upload_ids.append(target["id"])
    if isinstance(params.get("upload_id"), str):
        upload_ids.append(params["upload_id"])
    if upload_ids:
        from . import uploads

        for upload_id in dict.fromkeys(upload_ids):
            upload_owner = uploads.owner(upload_id)
            if upload_owner is None:
                return _error("загруженный файл не найден", 404)
            if restricted and upload_owner != owner:
                return _error("чужая загрузка: нет доступа", 403)
    try:
        job, deduplicated = scheduler.submit(JobSpec(
            type=job_type, target=target, params=params, owner=owner,
            priority=priority, depends_on=depends_on,
            idempotency_key=idempotency_key))
    except KeyError as exc:
        return _error(str(exc), 400)
    except (JobCycleError, ValueError) as exc:
        return _error(str(exc), 400)
    return JsonResponse({"job": _serialize(job, scheduler),
                         "deduplicated": deduplicated}, status=202)


def _result_shared(job, scheduler) -> bool:
    """На тот же файл кэша ссылается другая операция?

    Один архив переиспользуется разными запусками одной версии. Удалять
    файл вместе с первой записью нельзя: вторая перестанет скачиваться.
    """
    result = job.result or {}
    cache = result.get("cache")
    path = result.get("download_path")
    for other in scheduler.all():
        other_result = other.result or {}
        if cache and other_result.get("cache") == cache:
            return True
        if path and other_result.get("download_path") == path:
            return True
    return False


def _remove_result_file(job) -> None:
    """Стирает файл результата, если он лежит в кэше архивов.

    Нарезки фрагментов — конечный продукт задачи, они остаются. Кэш
    бэкапов и разметки — производные данные, его можно удалить вручную.
    """
    result = job.result or {}
    cache_info = result.get("cache")
    archives = os.path.join(vc_jobs.state_root(), "archives")
    if isinstance(cache_info, dict):
        from .archive_cache import ArchiveCache

        ArchiveCache(archives).remove(
            str(cache_info.get("namespace") or ""),
            str(cache_info.get("key") or ""),
        )
        return
    path = result.get("download_path")
    if not isinstance(path, str) or not path:
        return
    root = os.path.realpath(archives)
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep):
        return
    try:
        os.remove(real)
    except OSError:
        pass


@require_http_methods(["GET", "DELETE"])
def job_detail(request, job_id: str):
    scheduler = vc_jobs.get_scheduler()
    job = _job_or_404(scheduler, job_id, _owner(request))
    if job is None:
        return _error("операция не найдена", 404)
    if request.method == "DELETE":
        try:
            removed = scheduler.delete(job_id)
        except KeyError:
            return _error("операция не найдена", 404)
        except JobConflict as exc:
            return _error(str(exc), 409)
        if not _result_shared(removed, scheduler):
            _remove_result_file(removed)
        return JsonResponse({"deleted": removed.id})
    return JsonResponse({"job": _serialize(job, scheduler)})


def _set_hidden(request, job_id: str, hidden: bool) -> JsonResponse:
    scheduler = vc_jobs.get_scheduler()
    if _job_or_404(scheduler, job_id, _owner(request)) is None:
        return _error("операция не найдена", 404)
    try:
        job = scheduler.set_hidden(job_id, hidden)
    except KeyError:
        return _error("операция не найдена", 404)
    except JobConflict as exc:
        return _error(str(exc), 409)
    return JsonResponse({"job": _serialize(job, scheduler)})


@require_http_methods(["POST"])
def job_hide(request, job_id: str):
    """Скрыть терминальную операцию из рабочего списка (история остаётся)."""
    return _set_hidden(request, job_id, True)


@require_http_methods(["POST"])
def job_show(request, job_id: str):
    """Вернуть скрытую операцию в рабочий список."""
    return _set_hidden(request, job_id, False)


@require_http_methods(["POST"])
def job_cancel(request, job_id: str):
    scheduler = vc_jobs.get_scheduler()
    if _job_or_404(scheduler, job_id, _owner(request)) is None:
        return _error("операция не найдена", 404)
    try:
        job = scheduler.cancel(job_id)
    except KeyError:
        return _error("операция не найдена", 404)
    except JobConflict as exc:
        return _error(str(exc), 409)
    return JsonResponse({"job": _serialize(job, scheduler)}, status=202)


@require_http_methods(["POST"])
def job_retry(request, job_id: str):
    scheduler = vc_jobs.get_scheduler()
    if _job_or_404(scheduler, job_id, _owner(request)) is None:
        return _error("операция не найдена", 404)
    try:
        job = scheduler.retry(job_id)
    except KeyError:
        return _error("операция не найдена", 404)
    except JobConflict as exc:
        return _error(str(exc), 409)
    return JsonResponse({"job": _serialize(job, scheduler)}, status=202)


def _allowed_download(path: str) -> bool:
    real = os.path.realpath(path)
    roots = [
        os.path.realpath(workspace.WORKSPACE_ROOT),
        os.path.realpath(os.path.join(vc_jobs.state_root(), "archives")),
    ]
    return any(real == root or real.startswith(root + os.sep)
               for root in roots)


@require_http_methods(["GET"])
def job_download(request, job_id: str):
    scheduler = vc_jobs.get_scheduler()
    job = _job_or_404(scheduler, job_id, _owner(request))
    if job is None:
        return _error("операция не найдена", 404)
    if job.status != DONE:
        return _error("операция ещё не завершена", 409)
    result = job.result or {}
    path = result.get("download_path")
    if not path:
        return _error("у операции нет файла для скачивания", 409)
    expires = parse_iso((job.retention or {}).get("expires_at"))
    if expires is not None and expires < datetime.now(timezone.utc):
        return _error("результат истёк, запустите операцию заново", 410)
    cache_info = result.get("cache")
    if isinstance(cache_info, dict):
        job_type = scheduler.registry.get(job.type)
        ttl_days = job_type.retention_days if job_type else None
        if ttl_days is not None:
            from .archive_cache import ArchiveCache

            cache = ArchiveCache(os.path.join(
                vc_jobs.state_root(), "archives"))
            entry = cache.touch(
                str(cache_info.get("namespace")),
                str(cache_info.get("key")), float(ttl_days))
            if entry is None:
                return _error("результат истёк, запустите операцию заново",
                              410)
            scheduler.set_retention(job.id, entry.expires_at)
            path = entry.archive_path
    if not _allowed_download(path):
        return _error("путь результата вне разрешённых каталогов", 403)
    if not os.path.isfile(path):
        return _error("файл результата не найден", 404)
    name = os.path.basename(result.get("download_name") or path)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=name)


@require_http_methods(["POST"])
def jobs_upload(request):
    """Принимает архив для фонового импорта; возвращает upload_id."""
    from . import disk, uploads

    upload = request.FILES.get("file")
    if upload is None:
        return _error("Не передан файл архива (поле 'file')")
    owner, _roles, _restricted = _owner(request)
    try:
        body = uploads.save(upload, owner=owner)
    except disk.InsufficientSpace as exc:
        return _error(str(exc), 507)
    except ValueError as exc:
        return _error(str(exc))
    return JsonResponse(body, status=201)


@require_http_methods(["GET"])
def jobs_summary(request):
    scheduler = vc_jobs.get_scheduler()
    identity = _owner(request)
    counts = {status: 0 for status in STATUSES}
    for job in scheduler.all():
        if _visible(job, identity):
            counts[job.status] = counts.get(job.status, 0) + 1
    counts["active"] = counts["queued"] + counts["running"]
    return JsonResponse(counts)


@require_http_methods(["GET"])
def job_types(request):
    scheduler = vc_jobs.get_scheduler()
    return JsonResponse({"types": scheduler.registry.manifest()})
