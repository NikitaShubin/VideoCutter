# -*- coding: utf-8 -*-
"""Workspace views: список, детали, кадры, метаданные, создание/удаление.

Никакой БД и никакого DRF — всё через workspace.py + frame_provider.

Создание/удаление workspace — операции файловой системы. Их реализация живёт
в автономной обвязке (``videocutter.standalone.workspace``); здесь — тонкий
HTTP-слой и работа с процессным реестром workspace-ов (кэш Django-процесса).
"""

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

import workspace as ws_module
from videocutter.standalone import workspace as ws_fs
from vc_pairs import frame_provider
from workspace import get_workspace, get_workspace_detail, list_workspaces, scan_workspaces


def _ws_404(name: str) -> JsonResponse:
    return JsonResponse({"error": f"Workspace '{name}' не найден"}, status=404)


@require_http_methods(["GET", "POST"])
def workspace_list(request):
    """GET — список workspace-ов; POST — создать workspace из загруженного видео."""
    if request.method == "GET":
        return JsonResponse(list_workspaces(), safe=False)
    return _workspace_upload(request)


@require_http_methods(["GET", "DELETE"])
def workspace_detail(request, workspace_id: str):
    """GET — detail workspace; DELETE — безвозвратно удалить workspace."""
    if request.method == "GET":
        data = get_workspace_detail(workspace_id)
        if data is None:
            return _ws_404(workspace_id)
        return JsonResponse(data)
    return _workspace_delete(workspace_id)


def _workspace_upload(request) -> JsonResponse:
    """Создаёт workspace из multipart-загрузки (поле ``file``, опц. ``name``)."""
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "Не передан файл видео (поле 'file')"}, status=400)

    raw_name = request.POST.get("name") or ws_fs.default_workspace_name(upload.name)
    try:
        name = ws_fs.sanitize_workspace_name(raw_name)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=400)

    if get_workspace(name) is not None:
        return JsonResponse({"error": f"Workspace '{name}' уже существует"}, status=409)

    try:
        ws_fs.create_workspace(ws_module.WORKSPACE_ROOT, name, upload, upload.name)
    except ws_fs.WorkspaceExistsError as e:
        return JsonResponse({"error": str(e)}, status=409)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Валидация: файл должен читаться как видео, иначе откатываем создание.
    ws = get_workspace(name)
    try:
        if ws is None:
            raise ValueError("workspace не создан")
        meta = ws.metadata()
        if not meta.get("total_frames"):
            raise ValueError("видео не содержит кадров")
    except Exception as e:  # noqa: BLE001 — любой сбой чтения = битый файл
        ws_fs.delete_workspace(ws_module.WORKSPACE_ROOT, name)
        scan_workspaces()
        return JsonResponse({"error": f"Не удалось прочитать видео: {e}"}, status=400)

    entry = next((w for w in list_workspaces() if w["id"] == name), None)
    return JsonResponse(entry or {"id": name}, status=201)


def _workspace_delete(name: str) -> JsonResponse:
    """Удаляет workspace и чистит процессные кэши (кадры, экспорт)."""
    ws = get_workspace(name)
    if ws is None:
        return _ws_404(name)

    # Закрываем провайдеры кадров до удаления файлов.
    for path in {ws.original, ws.visualization}:
        if path:
            frame_provider.close_source(path)

    try:
        ws_fs.delete_workspace(ws_module.WORKSPACE_ROOT, name)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=404)

    from vc_fragments.views import EXPORTS, EXPORTS_LOCK

    with EXPORTS_LOCK:
        EXPORTS.pop(name, None)

    scan_workspaces()
    return JsonResponse({"deleted": name})


@require_http_methods(["GET"])
def workspace_frame(request, workspace_id: str, index: int):
    """Возвращает JPEG кадра по индексу (0-based)."""
    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)

    kind = request.GET.get("video", "visualization")
    path = ws.original if kind == "original" else (ws.visualization or ws.original)
    if not path:
        return JsonResponse({"error": "Видео не найдено в workspace"}, status=404)

    jpeg, mime = frame_provider.get_frame_jpeg(path, int(index))
    if jpeg is None:
        meta = ws.metadata()
        return JsonResponse(
            {"error": f"Кадр {index} недоступен (всего кадров: {meta['total_frames']})"},
            status=404,
        )
    response = HttpResponse(jpeg, content_type=mime)
    # Кадры неизменны в рамках сессии: браузер кэширует сам и снимает
    # нагрузку с бэкенда при перемотке назад (клиентский кэш — 60 кадров).
    response["Cache-Control"] = "private, max-age=3600"
    return response


@require_http_methods(["GET"])
def workspace_meta(request, workspace_id: str):
    """Метаданные workspace (без фрагментов)."""
    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)
    meta = ws.metadata()
    return JsonResponse({
        "id": workspace_id,
        "total_frames": meta["total_frames"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
    })
