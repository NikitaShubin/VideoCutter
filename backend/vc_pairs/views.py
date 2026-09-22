# -*- coding: utf-8 -*-
"""Workspace views: список, детали, кадры, метаданные, создание/удаление.

Никакой БД и никакого DRF — всё через workspace.py + frame_provider.

Создание/удаление workspace — операции файловой системы. Их реализация живёт
в автономной обвязке (``videocutter.standalone.workspace``); здесь — тонкий
HTTP-слой и работа с процессным реестром workspace-ов (кэш Django-процесса).
"""

import json
import os
import time

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

import workspace as ws_module
from videocutter.standalone import workspace as ws_fs
from vc_pairs import frame_provider
from workspace import (
    bump_updated_at,
    get_workspace,
    get_workspace_detail,
    list_workspaces,
    scan_workspaces,
)


def _ws_404(name: str) -> JsonResponse:
    return JsonResponse({"error": f"Workspace '{name}' не найден"}, status=404)


# Удаление: попыток и пауза между ними (FUSE + открытые хендлы).
_DELETE_ATTEMPTS = 4
_DELETE_RETRY_DELAY = 2.0


@require_http_methods(["GET", "POST"])
def workspace_list(request):
    """GET — список workspace-ов; POST — создать workspace из загруженного видео."""
    if request.method == "GET":
        return JsonResponse(list_workspaces(), safe=False)
    return _workspace_upload(request)


@require_http_methods(["GET", "PATCH", "DELETE"])
def workspace_detail(request, workspace_id: str):
    """GET — detail workspace; PATCH — переименовать; DELETE — безвозвратно удалить."""
    if request.method == "GET":
        data = get_workspace_detail(workspace_id)
        if data is None:
            return _ws_404(workspace_id)
        # Прогрев первой GOP к открытию редактора (фон, без ожидания).
        ws = get_workspace(workspace_id)
        if ws is not None:
            warm_path = ws.visualization or ws.original
            if warm_path:
                frame_provider.warm_gop(warm_path)
        return JsonResponse(data)
    if request.method == "PATCH":
        return _workspace_rename(request, workspace_id)
    return _workspace_delete(workspace_id)


def _workspace_upload(request) -> JsonResponse:
    """Создаёт workspace из multipart-полей ``source``/``preview`` (или ``file``).

    Поля ``source``/``preview`` — файлы видео (роль задаётся полем); хотя бы
    одно обязательно. Файлы сохраняются с ролевыми именами ``source.<ext>`` /
    ``preview.<ext>`` (расширение сохраняется), оригинальное имя уходит в имя
    workspace. Опц. ``name``.
    """
    src_file = request.FILES.get("source") or request.FILES.get("file")
    pv_file = request.FILES.get("preview")
    if src_file is None and pv_file is None:
        return JsonResponse(
            {"error": "Не передан файл видео (поля 'source' и/или 'preview')"},
            status=400,
        )

    raw_name = request.POST.get("name") or ws_fs.default_workspace_name(
        (src_file if src_file is not None else pv_file).name
    )
    try:
        name = ws_fs.sanitize_workspace_name(raw_name)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=400)

    if get_workspace(name) is not None:
        return JsonResponse({"error": f"Workspace '{name}' уже существует"}, status=409)

    try:
        ws_fs.create_workspace_pair(
            ws_module.WORKSPACE_ROOT,
            name,
            source=(src_file, src_file.name) if src_file else None,
            preview=(pv_file, pv_file.name) if pv_file else None,
        )
    except ws_fs.WorkspaceExistsError as e:
        return JsonResponse({"error": str(e)}, status=409)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=400)

    # Валидация: каждый файл должен читаться как видео, иначе откат создания.
    ws = get_workspace(name)
    try:
        if ws is None:
            raise ValueError("workspace не создан")
        roles = {ws.original, ws.visualization}
        if not any(roles):
            raise ValueError("видео не распознано (расширение файла не видео)")
        for path in roles:
            if path and not frame_provider.get_metadata(path).get("total_frames"):
                raise ValueError("видео не содержит кадров")
    except Exception as e:  # noqa: BLE001 — любой сбой чтения = битый файл
        ws_fs.delete_workspace(ws_module.WORKSPACE_ROOT, name)
        scan_workspaces()
        return JsonResponse({"error": f"Не удалось прочитать видео: {e}"}, status=400)

    entry = next((w for w in list_workspaces() if w["id"] == name), None)
    return JsonResponse(entry or {"id": name}, status=201)


def _workspace_rename(request, name: str) -> JsonResponse:
    """Переименовывает workspace (PATCH {"name": "новое"})."""
    try:
        payload = json.loads(request.body or "null")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    new_name = payload.get("name") if isinstance(payload, dict) else None

    from vc_fragments.views import EXPORTS, EXPORTS_LOCK

    # Переименование под работающим ffmpeg убивает экспорт (пути захвачены
    # потоком): требуем дождаться завершения или отмены.
    with EXPORTS_LOCK:
        if EXPORTS.get(name, {}).get("state") == "running":
            return JsonResponse(
                {"error": "Экспорт выполняется — дождитесь завершения или отмените его"},
                status=409,
            )
    try:
        renamed = ws_fs.rename_workspace(ws_module.WORKSPACE_ROOT, name, new_name)
    except ws_fs.WorkspaceExistsError as e:
        return JsonResponse({"error": str(e)}, status=409)
    except ws_fs.InvalidWorkspaceError as e:
        is_not_found = "не найден" in str(e)
        return JsonResponse({"error": str(e)}, status=404 if is_not_found else 400)

    if renamed != name:
        # Освобождаем провайдеры кадров (перезаймётся по новому имени файлов —
        # здесь пути не меняются, только имя каталога, поэтому кэш кадров жив).
        with EXPORTS_LOCK:
            if name in EXPORTS:
                EXPORTS[renamed] = EXPORTS.pop(name)
        scan_workspaces()
    else:
        scan_workspaces()

    data = get_workspace_detail(renamed)
    return JsonResponse(data if data is not None else {"id": renamed})


def _workspace_delete(name: str) -> JsonResponse:
    """Удаляет workspace и чистит процессные кэши (кадры, экспорт)."""
    ws = get_workspace(name)
    if ws is None:
        return _ws_404(name)

    # Закрываем провайдеры кадров до удаления файлов.
    for path in {ws.original, ws.visualization}:
        if path:
            frame_provider.close_source(path)

    from vc_fragments.views import EXPORTS, EXPORTS_LOCK, EXPORT_CANCEL

    # Останавливаем фон экспорта (проверяется между фрагментами).
    with EXPORTS_LOCK:
        if EXPORTS.get(name, {}).get("state") == "running":
            EXPORT_CANCEL.add(name)

    # На FUSE удаление при открытых хендлах (декод/префетч в полёте) оставляет
    # .fuse_hidden и роняет rmdir (ENOTEMPTY): повторяем с паузой, чтобы
    # in-flight декоды успели закрыть файлы. Причина всегда в ответе.
    last_err = None
    for _ in range(_DELETE_ATTEMPTS):
        try:
            ws_fs.delete_workspace(ws_module.WORKSPACE_ROOT, name)
            last_err = None
            break
        except ws_fs.InvalidWorkspaceError as e:
            return JsonResponse({"error": str(e)}, status=404)
        except OSError as e:
            last_err = e
            time.sleep(_DELETE_RETRY_DELAY)
    if last_err is not None:
        return JsonResponse(
            {"error": f"Не удалось удалить '{name}': {last_err}"}, status=500)

    with EXPORTS_LOCK:
        EXPORTS.pop(name, None)

    scan_workspaces()
    return JsonResponse({"deleted": name})


@require_http_methods(["GET"])
def workspace_frame(request, workspace_id: str, index: int):
    """Возвращает JPEG кадра по индексу (0-based).

    Опциональные перс-параметры: ``quality`` (20..95) и ``scale`` (0.05..1.0) —
    масштаб и качество JPEG «на лету», для быстрого просмотра больших видео.
    """
    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)

    kind = request.GET.get("video", "visualization")
    path = ws.original if kind == "original" else (ws.visualization or ws.original)
    if not path:
        return JsonResponse({"error": "Видео не найдено в workspace"}, status=404)

    try:
        quality = frame_provider.QUALITY_MAX
        if request.GET.get("quality"):
            quality = min(frame_provider.QUALITY_MAX,
                          max(frame_provider.QUALITY_MIN, int(request.GET["quality"])))
        scale = 1.0
        if request.GET.get("scale"):
            scale = min(frame_provider.SCALE_MAX,
                        max(frame_provider.SCALE_MIN, float(request.GET["scale"])))
    except ValueError:
        return JsonResponse({"error": "Некорректные параметры quality/scale"}, status=400)

    t0 = time.monotonic()
    jpeg, mime, info = frame_provider.get_frame_jpeg(
        path, int(index), quality=quality, scale=scale, want_info=True)
    if jpeg is None:
        meta = ws.metadata()
        return JsonResponse(
            {"error": f"Кадр {index} недоступен (всего кадров: {meta['total_frames']})"},
            status=404,
        )
    response = HttpResponse(jpeg, content_type=mime)
    # Наблюдаемость: источник кадра и время отдачи (диагностика stall'ов).
    response["X-Cache"] = info.get("source", "?")
    response["X-Decode-Ms"] = str(int((time.monotonic() - t0) * 1000))
    if info.get("abandoned"):
        response["X-Abandoned"] = "1"
    # Кадры неизменны в рамках сессии и параметров просмотра: браузер кэширует
    # сам и снимает нагрузку с бэкенда при перемотке назад (клиентский кэш — 60
    # кадров). URL кадра включает video_ver, поэтому замена файла меняет ключ.
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


# ─── Роли видео (source/preview): добавить, заменить, удалить, поменять ──────

def _drop_pending(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _entry_response(workspace_id: str) -> JsonResponse:
    entry = next((w for w in list_workspaces() if w["id"] == workspace_id), None)
    return JsonResponse(entry if entry is not None else {"id": workspace_id})


@require_http_methods(["POST", "DELETE"])
def workspace_video_role(request, workspace_id: str, role: str):
    """POST — добавить/заменить файл роли (multipart ``file``, опц. ``existing``).

    Django 4.2 не заполняет ``request.FILES`` для PUT/PATCH, поэтому аплоад
    выполняется методом POST (как и создание workspace).

    ``existing`` — роль, которой назначается прежний «нейтральный» файл при
    добавлении второго видео (сценарий: был один файл — теперь два).

    DELETE — убрать ролевой файл (нельзя оставить workspace без видео).
    """
    if role not in ws_fs.ROLES:
        return JsonResponse({"error": f"Неизвестная роль: {role!r}"}, status=400)

    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)

    if request.method == "POST":
        return _put_role(request, ws, role)
    return _delete_role(ws, role)


def _put_role(request, ws, role: str) -> JsonResponse:
    """Добавляет/заменяет файл роли или назначает роль существующему файлу.

    Два режима (нельзя передавать оба):
      * ``file`` (multipart) — новое видео; опционально ``existing``: роль,
        которой назначается прежний «нейтральный» файл при добавлении второго
        видео (сценарий: был один файл — теперь два);
      * ``assign=<имя файла>`` — назначить роль уже лежащему в workspace
        «неразмеченному» видео (unassigned) без новой загрузки.
    """
    assign = request.POST.get("assign")
    upload = request.FILES.get("file")
    existing = request.POST.get("existing")

    if assign is not None and upload is not None:
        return JsonResponse(
            {"error": "Нельзя передавать и 'assign', и загружаемый файл"}, status=400
        )

    if assign is not None:
        try:
            final = ws_fs.assign_role_file(
                ws_module.WORKSPACE_ROOT, ws.name, role, assign
            )
        except ws_fs.InvalidWorkspaceError as e:
            return JsonResponse({"error": str(e)}, status=400)
        self_closed = {ws.original, ws.visualization}
        ws.invalidate_videos()
        scan_workspaces()
        bump_updated_at(ws)
        for path in self_closed | {final}:
            if path:
                frame_provider.close_source(path)
        return _entry_response(ws.name)

    if upload is None:
        return JsonResponse({"error": "Не передан файл (поле 'file')"}, status=400)

    if existing is not None and existing not in ws_fs.ROLES:
        return JsonResponse(
            {"error": f"Неизвестная роль existing: {existing!r}"}, status=400
        )
    if existing == role:
        return JsonResponse(
            {"error": "existing должен быть ролью, противоположной добавляемой"},
            status=400,
        )

    before = {"source": ws.original, "preview": ws.visualization}

    # Загрузка во временный файл + валидация содержимого (не по расширению).
    tmp = ws_fs.make_upload_temp(ws_module.WORKSPACE_ROOT, ws.name)
    try:
        ws_fs.write_stream(upload, tmp)
        if not frame_provider.get_metadata(tmp).get("total_frames"):
            raise ValueError("видео не содержит кадров")
    except Exception as e:  # noqa: BLE001 — любой сбой чтения = битый файл
        _drop_pending(tmp)
        return JsonResponse({"error": f"Не удалось прочитать видео: {e}"}, status=400)

    promoted = None
    try:
        if existing:
            promoted = ws_fs.promote_plain_video(
                ws_module.WORKSPACE_ROOT, ws.name, existing
            )
            if promoted is None:
                raise ws_fs.InvalidWorkspaceError(
                    "Нет нейтрального видео, чтобы назначить ему роль existing"
                )
        final = ws_fs.commit_role_upload(
            ws_module.WORKSPACE_ROOT, ws.name, role, tmp, upload.name
        )
    except ws_fs.InvalidWorkspaceError as e:
        _drop_pending(tmp)
        return JsonResponse({"error": str(e)}, status=400)

    # Дропаем провайдеры кадров заменённых/удалённых/временных путей.
    for path in {before["source"], before["preview"], tmp, final, promoted}:
        if path:
            frame_provider.close_source(path)

    ws.invalidate_videos()
    scan_workspaces()
    bump_updated_at(ws)
    return _entry_response(ws.name)


def _delete_role(ws, role: str) -> JsonResponse:
    if ws.original == ws.visualization:
        return JsonResponse(
            {"error": "Нельзя удалить единственное видео workspace"}, status=400
        )
    old = ws.original if role == "source" else ws.visualization
    removed = ws_fs.remove_role_file(ws_module.WORKSPACE_ROOT, ws.name, role)
    if removed is None:
        return JsonResponse(
            {"error": f"Роль '{role}' не выделена явно файлом"}, status=400
        )
    if old and os.path.isfile(old):
        frame_provider.close_source(old)

    ws.invalidate_videos()
    scan_workspaces()
    bump_updated_at(ws)
    return _entry_response(ws.name)


@require_http_methods(["POST"])
def workspace_swap(request, workspace_id: str):
    """Меняет роли двух видео местами (source <-> preview)."""
    ws = get_workspace(workspace_id)
    if ws is None:
        return _ws_404(workspace_id)

    s_path, p_path = ws.original, ws.visualization
    if not s_path or not p_path or s_path == p_path:
        return JsonResponse(
            {"error": "Для смены ролей нужно два разных видео"}, status=400
        )

    # Промотируем неявные роли до явных, затем обмениваем файлы.
    try:
        if ws_fs.role_of_name(os.path.basename(s_path)) != "source":
            ws_fs.promote_plain_video(ws_module.WORKSPACE_ROOT, ws.name, "source")
        if ws_fs.role_of_name(os.path.basename(p_path)) != "preview":
            ws_fs.promote_plain_video(ws_module.WORKSPACE_ROOT, ws.name, "preview")
        ws_fs.swap_role_files(ws_module.WORKSPACE_ROOT, ws.name)
    except ws_fs.InvalidWorkspaceError as e:
        return JsonResponse({"error": str(e)}, status=400)

    for path in {s_path, p_path}:
        if path:
            frame_provider.close_source(path)

    ws.invalidate_videos()
    scan_workspaces()
    bump_updated_at(ws)
    return _entry_response(ws.name)
