# -*- coding: utf-8 -*-
"""Fragment views: TSV-based (никакой БД, никакого DRF).

Фрагменты хранятся как fragments.tsv в директории workspace.
"""

import json
import os
import threading

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from videocutter.core.exporter import Exporter, FFmpegError
from vc_pairs import frame_provider
from workspace import get_workspace


# Приоритет процессов экспорта: низкий (только свободные CPU), интерактив
# (листинг/индексация/кадры) — обычный. Переопределяется env.
try:
    EXPORT_NICE = max(0, min(19, int(os.environ.get("VC_EXPORT_NICE", "19"))))
except (TypeError, ValueError):
    EXPORT_NICE = 19


# Прогресс экспортов в памяти процесса: workspace_id -> {"state": ...}.
EXPORTS: dict[str, dict] = {}
EXPORTS_LOCK = threading.Lock()
# Запросы на отмену: cancel ставится кнопкой/клавишей во время running;
# фоновый поток проверяет флаг перед каждым фрагментом и прерывается.
EXPORT_CANCEL: set[str] = set()


class _ExportCancelled(Exception):
    """Флаг отмены: экспорт остановлен пользователем между фрагментами."""


# Sidecar готового экспорта (рядом с файлами): переживает рестарт процесса.
# Без него повторное открытие задачи после рестарта видело бы idle и гнало
# экспорт заново, хотя файлы на месте.
_SIDECAR = ".export-state.json"


def _fragments_sig(frags) -> str:
    """Сигнатура границ [[start,end],...] — формат 1-в-1 с клиентом."""
    return json.dumps([[f["start"], f["end"]] for f in frags],
                      separators=(",", ":"))


def _export_finished_ok(ws_id: str, created: list[str], sig: str) -> None:
    urls = []
    for i, path in enumerate(created, 1):
        filename = os.path.basename(path)
        urls.append({
            "index": i,
            "filename": filename,
            "url": f"/api/v1/pairs/{ws_id}/export/{filename}",
        })
    with EXPORTS_LOCK:
        EXPORTS[ws_id] = {"state": "done", "files": urls, "sig": sig}
    # Sidecar для восстановления статуса после рестарта (имена файлов без
    # ws_id — переименование задачи его не инвалидирует).
    if created:
        try:
            with open(os.path.join(os.path.dirname(created[0]), _SIDECAR),
                      "w", encoding="utf-8") as f:
                json.dump({
                    "sig": sig,
                    "files": [os.path.basename(p) for p in created],
                }, f)
        except OSError:
            pass


def _export_failed(ws_id: str, message: str) -> None:
    with EXPORTS_LOCK:
        EXPORTS[ws_id] = {"state": "error", "error": message}


def _export_cancelled(ws_id: str, created_now: list[str]) -> None:
    """Отмена пользователем: зачищаем частичные файлы этого запуска."""
    for path in created_now:
        try:
            os.remove(path)
        except OSError:
            pass
    with EXPORTS_LOCK:
        EXPORTS[ws_id] = {"state": "cancelled"}
        EXPORT_CANCEL.discard(ws_id)


def _run_export(ws_id: str) -> None:
    """Выполняет нарезку фрагментов в фоне."""
    try:
        ws = get_workspace(ws_id)
        if ws is None:
            _export_failed(ws_id, f"Workspace '{ws_id}' не найден")
            return

        frags = ws.load_fragments()
        if not frags:
            _export_failed(ws_id, "Нет фрагментов для экспорта")
            return

        out_dir = os.path.join(ws.path, "exports")
        os.makedirs(out_dir, exist_ok=True)
        before = set(os.listdir(out_dir))

        exporter = Exporter(ws.original, out_dir, nice=EXPORT_NICE)
        fragments = [(f["start"], f["end"]) for f in frags]
        total = len(fragments)
        def progress(fragment_ind: int, _total: int, _fragment) -> None:
            with EXPORTS_LOCK:
                if ws_id in EXPORT_CANCEL:
                    raise _ExportCancelled()
                EXPORTS[ws_id] = {
                    "state": "running",
                    "index": fragment_ind,
                    "total": total,
                }

        created = exporter.extract_fragments(fragments, progress=progress)
        sig = _fragments_sig(frags)
        _export_finished_ok(ws_id, created, sig)
    except _ExportCancelled:
        # Файлы, появившиеся за этот запуск (готовые фрагменты валидны,
        # но пользователь просил зачистку) — удаляем по снапшоту каталога.
        try:
            after = set(os.listdir(out_dir))
        except OSError:
            after = set()
        created_now = [os.path.join(out_dir, name) for name in after - before]
        _export_cancelled(ws_id, created_now)
    except FFmpegError as e:
        _export_failed(ws_id, str(e))
    except Exception as e:
        _export_failed(ws_id, str(e))
    finally:
        with EXPORTS_LOCK:
            EXPORT_CANCEL.discard(ws_id)


@require_http_methods(["GET", "PUT"])
def fragments(request, pair_id: str):
    """GET — список фрагментов; PUT — заменить список (list of {start,end,comment?})
    либо объект {"fragments": [...], "position": N} (позиция пишется в тот же файл)."""
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    if request.method == "GET":
        return JsonResponse(ws.load_fragments(), safe=False)

    # PUT: замена списка фрагментов (+ опционально позиции).
    try:
        payload = json.loads(request.body or "null")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    # Тело-объект несёт позицию; голый список — обратная совместимость (позиция
    # сохраняется из кэша).
    raw_position = None
    if isinstance(payload, dict):
        items = payload.get("fragments")
        raw_position = payload.get("position")
    else:
        items = payload

    if not isinstance(items, list):
        return JsonResponse({"error": "Ожидался список фрагментов"}, status=400)

    meta = ws.metadata()
    total_frames = meta["total_frames"]

    position = None
    if raw_position is not None:
        try:
            position = int(raw_position)
        except (TypeError, ValueError):
            return JsonResponse({"error": f"Неверная позиция: {raw_position}"}, status=400)
        if position < 0 or (total_frames > 0 and position >= total_frames):
            return JsonResponse(
                {"error": f"Позиция {position} вне диапазона [0, {total_frames})"},
                status=400,
            )

    # Читаем старые комментарии для обратной совместимости.
    old_comments = {(f["start"], f["end"]): f.get("comment", "") for f in ws.load_fragments()}

    clean = []
    for item in items:
        if not isinstance(item, dict):
            return JsonResponse({"error": f"Неверный фрагмент: {item}"}, status=400)
        try:
            start, end = int(item["start"]), int(item["end"])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({"error": f"Неверный фрагмент: {item}"}, status=400)
        if start < 0 or (total_frames > 0 and end >= total_frames) or start > end:
            return JsonResponse(
                {"error": f"Фрагмент {start}-{end} вне диапазона [0, {total_frames})"},
                status=400,
            )
        if "comment" in item and item["comment"] is not None:
            comment = str(item["comment"])
        else:
            comment = old_comments.get((start, end), "")
        clean.append({"start": start, "end": end, "comment": comment})

    # Проверка пересечений.
    clean.sort(key=lambda x: (x["start"], x["end"]))
    for i in range(1, len(clean)):
        if clean[i]["start"] <= clean[i - 1]["end"]:
            return JsonResponse({"error": "Фрагменты пересекаются"}, status=409)

    ws.save_fragments(clean, position)
    return JsonResponse(clean, safe=False)


@require_http_methods(["GET", "PUT"])
def pair_position(request, pair_id: str):
    """GET — текущий кадр; PUT {"position": N} — сохранить текущий кадр."""
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    if request.method == "GET":
        return JsonResponse({"position": ws.load_position()})

    try:
        payload = json.loads(request.body or "null")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    if not isinstance(payload, dict) or "position" not in payload:
        return JsonResponse({"error": "Ожидался объект {\"position\": N}"}, status=400)

    try:
        position = int(payload["position"])
    except (TypeError, ValueError):
        return JsonResponse({"error": f"Неверная позиция: {payload['position']}"}, status=400)

    total_frames = ws.metadata()["total_frames"]
    if position < 0 or (total_frames > 0 and position >= total_frames):
        return JsonResponse(
            {"error": f"Позиция {position} вне диапазона [0, {total_frames})"},
            status=400,
        )

    ws.save_position(position)
    return JsonResponse({"position": position})


@require_http_methods(["GET", "PUT"])
def pair_settings(request, pair_id: str):
    """GET — настройки просмотра; PUT {"quality": 20..95, "scale": 0.05..1.0}.

    Настройки view-качества (ползунки редактора) сохраняются в fragments.tsv
    строкой ``# settings\t<quality>\t<scale>`` — как и позиция, они
    восстанавливаются при каждом открытии задачи.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    if request.method == "GET":
        quality, scale = ws.load_settings()
        return JsonResponse({"quality": quality, "scale": scale})

    try:
        payload = json.loads(request.body or "null")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Некорректный JSON"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse(
            {"error": "Ожидался объект {\"quality\": 20..95, \"scale\": 0.05..1.0}"},
            status=400,
        )

    try:
        quality = int(payload.get("quality", ws.load_settings()[0]))
        scale = float(payload.get("scale", ws.load_settings()[1]))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Неверные quality/scale"}, status=400)

    if not (frame_provider.QUALITY_MIN <= quality <= frame_provider.QUALITY_MAX):
        return JsonResponse(
            {"error": f"quality вне диапазона [{frame_provider.QUALITY_MIN}, {frame_provider.QUALITY_MAX}]"},
            status=400,
        )
    if not (frame_provider.SCALE_MIN <= scale <= frame_provider.SCALE_MAX):
        return JsonResponse(
            {"error": f"scale вне диапазона [{frame_provider.SCALE_MIN}, {frame_provider.SCALE_MAX}]"},
            status=400,
        )

    ws.save_settings(quality, scale)
    return JsonResponse({"quality": quality, "scale": scale})


@require_http_methods(["POST"])
def fragment_export(request, pair_id: str):
    """Запускает ffmpeg-нарезку в фоне; прогресс — через status."""
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    frags = ws.load_fragments()
    if not frags:
        return JsonResponse({"error": "Нет фрагментов для экспорта"}, status=400)

    with EXPORTS_LOCK:
        current = EXPORTS.get(pair_id)
        if current and current["state"] == "running":
            return JsonResponse(
                {"error": "Экспорт уже выполняется для этого workspace"},
                status=409,
            )
        EXPORTS[pair_id] = {"state": "running", "index": 0, "total": len(frags)}

    threading.Thread(target=_run_export, args=(pair_id,), daemon=True).start()
    return JsonResponse({"state": "running", "index": 0, "total": len(frags)})


@require_http_methods(["POST"])
def fragment_export_cancel(request, pair_id: str):
    """Просит фоновый экспорт остановиться (срабатывает между фрагментами).

    Частичные файлы этого запуска зачищаются, состояние — "cancelled".
    Отмена не бегущего экспорта — 409.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    with EXPORTS_LOCK:
        current = EXPORTS.get(pair_id)
        if not current or current["state"] != "running":
            return JsonResponse(
                {"error": "Экспорт не выполняется для этого workspace"},
                status=409,
            )
        EXPORT_CANCEL.add(pair_id)
    return JsonResponse({"state": "cancelling"})


@require_GET
def fragment_export_status(request, pair_id: str):
    """Текущий статус экспорта."""
    with EXPORTS_LOCK:
        state = EXPORTS.get(pair_id)
    if not state:
        # Памяти нет (рестарт?) — сверяемся с диском: готовые файлы + тот же
        # набор фрагментов = done без повторного прогона.
        state = _status_from_disk(pair_id)
    if not state:
        return JsonResponse({"state": "idle"})
    if "files" in state:
        body = {"state": "done", "files": state["files"]}
        if state.get("sig"):
            body["sig"] = state["sig"]
        return JsonResponse(body)
    if state["state"] == "error":
        return JsonResponse({"state": "error", "error": state.get("error", "Ошибка экспорта")})
    if state["state"] in ("cancelled", "cancelling"):
        return JsonResponse({"state": state["state"]})
    return JsonResponse({
        "state": "running",
        "index": state.get("index", 0),
        "total": state.get("total", 1),
    })


def _status_from_disk(pair_id: str):
    """Статус done из sidecar (после рестарта) или None.

    Возвращает done, только если sidecar-сигнатура совпадает с текущими
    границами фрагментов и все файлы на месте.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return None
    out_dir = os.path.join(ws.path, "exports")
    try:
        with open(os.path.join(out_dir, _SIDECAR), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    frags = ws.load_fragments()
    if not frags or saved.get("sig") != _fragments_sig(frags):
        return None
    urls = []
    for i, filename in enumerate(saved.get("files") or [], 1):
        full = os.path.join(out_dir, os.path.basename(filename))
        try:
            # Нулевой размер — недописанный файл убитого экспорта, не готовый.
            if not os.path.isfile(full) or os.path.getsize(full) == 0:
                return None
        except OSError:
            return None
        urls.append({
            "index": i,
            "filename": os.path.basename(filename),
            "url": f"/api/v1/pairs/{pair_id}/export/{os.path.basename(filename)}",
        })
    if not urls:
        return None
    return {"state": "done", "files": urls, "sig": saved["sig"]}


@require_GET
def fragment_export_download(request, pair_id: str, path: str):
    """Скачивает экспортированный фрагмент."""
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    base = os.path.join(ws.path, "exports")
    full = os.path.abspath(os.path.join(base, path))
    if not full.startswith(os.path.abspath(base)) or not os.path.isfile(full):
        return JsonResponse({"error": "Файл не найден"}, status=404)
    return FileResponse(open(full, "rb"), as_attachment=True, filename=os.path.basename(full))