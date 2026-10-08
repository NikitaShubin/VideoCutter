# -*- coding: utf-8 -*-
"""Project views: список, CRUD, привязка/отвязка задач.

Файловая обвязка (project_meta) + работа с реестром workspace-ов.
Никакой БД и никакого DRF — как весь vc_pairs.

Модель снята с CVAT: проект и задача — независимые сущности, членство —
указатель ``project_id`` на задаче (attach/detach возможны в обе стороны);
``can_attach`` — точка будущих проверок совместимости (см. docs).
"""

import json
from typing import Dict, Optional

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

import project_meta
import task_meta
import workspace as ws_module
from videocutter.standalone import workspace as ws_fs
from workspace import scan_workspaces

from .views import _workspace_delete


def _project_404(pid: str) -> JsonResponse:
    return JsonResponse({"error": f"Проект '{pid}' не найден"}, status=404)


def _bad_request(message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=400)


def _load_json(request):
    """Тело запроса → dict (или None для пустого); битый JSON — ошибка."""
    try:
        payload = json.loads(request.body or "null")
    except json.JSONDecodeError:
        return None, _bad_request("Некорректный JSON")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return None, _bad_request("Ожидается JSON-объект")
    return payload, None


def _task_counts() -> Dict[str, int]:
    """Состав всех проектов за один скан указателей задач."""
    counts: Dict[str, int] = {}
    for pid in project_meta.task_project_ids().values():
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


def _project_entry(meta: dict, counts: Optional[Dict[str, int]] = None) -> dict:
    """project.json + счётчик задач (состав резолвится сканом указателей)."""
    out = dict(meta)
    out["task_count"] = (counts or _task_counts()).get(meta["id"], 0)
    return out


# ─── Список и создание ──────────────────────────────────────────────────────

@require_http_methods(["GET", "POST"])
def project_list(request):
    """GET — все проекты со счётчиками; POST — создать ({"name": ...})."""
    if request.method == "GET":
        # Скан до чтения реестра: первый проход по установке прогоняет
        # миграцию в default — список проектов обязан её увидеть, даже
        # если запросил раньше списка задач.
        scan_workspaces()
        counts = _task_counts()
        items = [_project_entry(m, counts) for m in project_meta.list_projects()]
        return JsonResponse(items, safe=False)

    payload, err = _load_json(request)
    if err is not None:
        return err
    name = (payload.get("name") or "").strip()
    if not name:
        return _bad_request("Не передано имя проекта (поле 'name')")
    try:
        meta = project_meta.create(name)
    except ws_fs.InvalidWorkspaceError as e:
        return _bad_request(str(e))
    except ws_fs.WorkspaceExistsError as e:
        return JsonResponse({"error": str(e)}, status=409)
    return JsonResponse(_project_entry(meta), status=201)


# ─── Деталь: чтение, переименование, удаление ───────────────────────────────

@require_http_methods(["GET", "PUT", "DELETE"])
def project_detail(request, project_id: str):
    """GET — проект; PUT — переименовать/править model; DELETE — удалить.

    DELETE принимает ``?with_tasks=``:
      * ``cascade`` (дефолт, как в CVAT) — удалить и задачи с видео;
      * ``keep`` — задачи остаются standalone;
      * ``move&to=<pid>`` — задачи переезжают в другой проект.
    """
    if request.method == "GET":
        if not project_meta.exists(project_id):
            return _project_404(project_id)
        return JsonResponse(_project_entry(project_meta.load(project_id)))
    if request.method == "PUT":
        return _project_update(request, project_id)
    return _project_delete(request, project_id)


def _project_update(request, pid: str) -> JsonResponse:
    if not project_meta.exists(pid):
        return _project_404(pid)
    payload, err = _load_json(request)
    if err is not None:
        return err
    unknown = set(payload) - {"name", "model"}
    if unknown:
        return _bad_request(f"Неизвестные поля: {', '.join(sorted(unknown))}")

    meta = project_meta.load(pid)
    if "name" in payload:
        name = payload["name"]
        if not isinstance(name, str) or not name.strip():
            return _bad_request("Имя проекта не может быть пустым")
        meta["name"] = name.strip()
    if "model" in payload:
        model = payload["model"]
        if not isinstance(model, dict):
            return _bad_request("model должен быть объектом")
        meta["model"] = model
    try:
        meta = project_meta.save(pid, meta)
    except ValueError as e:
        return _bad_request(str(e))
    return JsonResponse(_project_entry(meta))


def _project_delete(request, pid: str) -> JsonResponse:
    if not project_meta.exists(pid):
        return _project_404(pid)

    mode = request.GET.get("with_tasks", "cascade")
    if mode not in ("cascade", "keep", "move"):
        return _bad_request(
            "with_tasks: ожидается cascade | keep | move")
    target = request.GET.get("to")
    if mode == "move":
        if not target:
            return _bad_request("with_tasks=move требует &to=<pid>")
        if not project_meta.exists(target):
            return _project_404(target)

    members = [name for name, cur in project_meta.task_project_ids().items()
               if cur == pid]

    if mode == "cascade":
        # Каскад — как в CVAT: проект удаляется вместе с задачами (и видео).
        # Прерванный на полу пути оставляет проект: лучше сообщить об ошибке,
        # чем удалить пусто.
        failed = []
        for name in members:
            resp = _workspace_delete(name)
            if resp.status_code != 200:
                failed.append(f"{name}: {resp.content.decode('utf-8', 'replace')}")
        if failed:
            scan_workspaces()
            return JsonResponse(
                {"error": "Не все задачи удалены: " + "; ".join(failed)},
                status=500)
    elif mode == "keep":
        for name in members:
            project_meta.detach(name)
    else:  # move
        for name in members:
            project_meta.attach(name, target)

    try:
        project_meta.delete(pid)
    except (ws_fs.InvalidWorkspaceError, OSError) as e:
        scan_workspaces()
        return JsonResponse({"error": str(e)}, status=500)

    scan_workspaces()
    affected = {"cascade": {"deleted": len(members)},
                "keep": {"kept": len(members)},
                "move": {"moved": len(members), "to": target}}[mode]
    return JsonResponse({"deleted": pid, "tasks": affected})


# ─── Привязка / отвязка задач ───────────────────────────────────────────────

@require_http_methods(["POST"])
def project_attach(request, project_id: str):
    """POST {"task_id": ...} — привязать задачу (перевеска из другого проекта)."""
    if not project_meta.exists(project_id):
        return _project_404(project_id)
    payload, err = _load_json(request)
    if err is not None:
        return err
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        return _bad_request("Не передан task_id")
    problem = project_meta.can_attach(task_id, project_id)
    if problem is not None:
        status = 404 if "не найдена" in problem else 400
        return JsonResponse({"error": problem}, status=status)
    project_meta.attach(task_id, project_id)
    scan_workspaces()
    return JsonResponse({"task_id": task_id, "project_id": project_id})


@require_http_methods(["DELETE"])
def project_detach(request, project_id: str, task_id: str):
    """DELETE .../tasks/<task_id> — отвязать (задача становится standalone)."""
    if not project_meta.exists(project_id):
        return _project_404(project_id)
    try:
        ws_path = ws_fs.workspace_path(ws_module.WORKSPACE_ROOT, task_id)
    except ws_fs.InvalidWorkspaceError:
        return _bad_request(f"Некорректное имя задачи: {task_id!r}")
    current = task_meta.load(ws_path).get("project_id")
    if current != project_id:
        return JsonResponse(
            {"error": f"Задача '{task_id}' не в проекте '{project_id}'"},
            status=404)
    try:
        project_meta.detach(task_id)
    except ValueError as e:
        return _bad_request(str(e))
    scan_workspaces()
    return JsonResponse({"task_id": task_id, "project_id": None})
