# -*- coding: utf-8 -*-
"""Workspace views: список, детали, кадры, метаданные.

Никакой БД и никакого DRF — всё через workspace.py + frame_provider.
"""

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from vc_pairs import frame_provider
from workspace import get_workspace, get_workspace_detail, list_workspaces


def _ws_404(name: str) -> JsonResponse:
    return JsonResponse({"error": f"Workspace '{name}' не найден"}, status=404)


@require_GET
def workspace_list(request):
    """GET — список всех workspace-ов (метаданные без фрагментов)."""
    return JsonResponse(list_workspaces(), safe=False)


@require_GET
def workspace_detail(request, workspace_id: str):
    """GET — полный detail workspace (метаданные + фрагменты)."""
    data = get_workspace_detail(workspace_id)
    if data is None:
        return _ws_404(workspace_id)
    return JsonResponse(data)


@require_GET
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


@require_GET
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