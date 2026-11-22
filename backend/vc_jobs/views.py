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


def _job_or_404(scheduler, job_id: str, identity):
    job = scheduler.get(job_id)
    if job is None or not _visible(job, identity):
        return None
    return job


def _serialize(job) -> Dict[str, Any]:
    return {**job.to_dict(), "display_status": job.display_status}


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
                limit=limit,
                cursor=request.GET.get("cursor") or None,
            )
        except ValueError as exc:
            return _error(str(exc))
        owner, _roles, restricted = identity
        visible = [j for j in jobs if _visible(j, identity)]
        if restricted:
            # Чужой фильтр по владельцу не должен раскрывать чужое.
            if owner and any(j.owner != owner for j in visible):
                visible = [j for j in visible if j.owner == owner]
        return JsonResponse({
            "jobs": [_serialize(j) for j in visible],
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

    owner, _roles, _restricted = identity
    try:
        job, deduplicated = scheduler.submit(JobSpec(
            type=job_type, target=target, params=params, owner=owner,
            priority=priority, depends_on=depends_on,
            idempotency_key=idempotency_key))
    except KeyError as exc:
        return _error(str(exc), 400)
    except (JobCycleError, ValueError) as exc:
        return _error(str(exc), 400)
    return JsonResponse({"job": _serialize(job),
                         "deduplicated": deduplicated}, status=202)


@require_http_methods(["GET"])
def job_detail(request, job_id: str):
    scheduler = vc_jobs.get_scheduler()
    job = _job_or_404(scheduler, job_id, _owner(request))
    if job is None:
        return _error("операция не найдена", 404)
    return JsonResponse({"job": _serialize(job)})


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
    return JsonResponse({"job": _serialize(job)}, status=202)


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
    return JsonResponse({"job": _serialize(job)}, status=202)


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
    if not _allowed_download(path):
        return _error("путь результата вне разрешённых каталогов", 403)
    if not os.path.isfile(path):
        return _error("файл результата не найден", 404)
    name = os.path.basename(result.get("download_name") or path)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=name)


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
