# -*- coding: utf-8 -*-
"""Fragment views: TSV-based (никакой БД, никакого DRF).

Фрагменты хранятся как fragments.tsv в директории workspace.
"""

import hashlib
import json
import os
import subprocess
import threading

import cv2
import numpy as np
from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

from videocutter.core.exporter import (
    Exporter,
    ExportCancelled,
    FFmpegError,
)
from vc_pairs import frame_provider
from workspace import get_workspace


# Приоритет процессов экспорта: низкий (только свободные CPU), интерактив
# (листинг/индексация/кадры) — обычный. Переопределяется env.
try:
    EXPORT_NICE = max(0, min(19, int(os.environ.get("VC_EXPORT_NICE", "19"))))
except (TypeError, ValueError):
    EXPORT_NICE = 19

# Порог сверки контента нарезки: средний квадрат разности даунскейла.
# Своя (crf15) нарезка даёт доли единицы, чужой динамичный контент — тысячи;
# почти статичный чужой контент неразличим по построению (и безвреден).
VERIFY_SSD_MAX = float(os.environ.get("VC_EXPORT_VERIFY_SSD", "100"))


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


def _video_identity(path: str):
    """Идентичность видео {size, mtime_ns} (None — файла нет)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


# Допуск границ frame_ts-диапазона (c): float-неточность представления меток.
# На порядки меньше межкадрового интервала — соседние кадры не цепляет.
_FRAME_TS_EPS = 0.0005


def _frame_ts_bounds(vpts, start: int, end: int):
    """Секундный диапазон для кадров [start, end]: [t0-eps, t1-next-eps).

    Верхняя граница — следующий кадр минус eps (полуинтервал): кадр end
    входит точно, следующий — никогда, независимо от float-округлений.
    """
    t0 = vpts[start] / 1e6 - _FRAME_TS_EPS
    if end + 1 < len(vpts):
        t1 = vpts[end + 1] / 1e6 - _FRAME_TS_EPS
    else:
        t1 = vpts[end] / 1e6 + _FRAME_TS_EPS
    return (t0, t1)


def _task_hash(frags, size: int, mtime_ns: int) -> str:
    """Хеш состояния нарезки: границы (порядок не важен) + видео.

    Совпал — файлы соответствуют нынешнему состоянию, отдаём готовое.
    """
    canon = json.dumps({
        "fragments": sorted([f["start"], f["end"]] for f in frags),
        "video": {"size": size, "mtime_ns": mtime_ns},
    }, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _export_finished_ok(ws_id: str, created: list[str], task_hash) -> None:
    urls = []
    for i, path in enumerate(created, 1):
        filename = os.path.basename(path)
        urls.append({
            "index": i,
            "filename": filename,
            "url": f"/api/v1/pairs/{ws_id}/export/{filename}",
        })
    with EXPORTS_LOCK:
        EXPORTS[ws_id] = {"state": "done", "files": urls, "hash": task_hash}
    # Sidecar для восстановления статуса после рестарта (имена файлов без
    # ws_id — переименование задачи его не инвалидирует).
    if created:
        try:
            with open(os.path.join(os.path.dirname(created[0]), _SIDECAR),
                      "w", encoding="utf-8") as f:
                json.dump({
                    "hash": task_hash,
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


def _ffprobe_frame_count(path: str):
    """Число видеокадров файла (decode-подсчёт) или None."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames",
             "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return None


def _frame_small_at(path: str, index: int):
    """Кадр даунскейл-gray float32 (None — не декодируется)."""
    cap = cv2.VideoCapture(path)
    try:
        if index > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, fr = cap.read()
        if not ok or fr is None:
            return None
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
        return cv2.resize(g, (64, 36),
                         interpolation=cv2.INTER_AREA).astype(np.float32)
    finally:
        cap.release()


def _verify_cut(src_path: str, start_index: int, cut_path: str,
                expected_count: int) -> None:
    """Проверка нарезки: счётчик кадров + контент первого кадра.

    :raises FFmpegError: несовпадение — молчаливого успеха не будет.
    """
    actual = _ffprobe_frame_count(cut_path)
    if actual is None:
        raise FFmpegError(f"Не удалось посчитать кадры: {cut_path}")
    if actual != expected_count:
        raise FFmpegError(
            f"Кадров в нарезке: {actual}, ожидалось: {expected_count} "
            f"({cut_path})")
    exp = _frame_small_at(src_path, start_index)
    got = _frame_small_at(cut_path, 0)
    if exp is None or got is None:
        raise FFmpegError(f"Не декодируется кадр для сверки: {cut_path}")
    d = exp - got
    ssd = float((d * d).mean())
    if ssd > VERIFY_SSD_MAX:
        raise FFmpegError(
            f"Контент не совпал (ssd={ssd:.1f} > {VERIFY_SSD_MAX}): "
            f"{cut_path}")


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

        # Секундные диапазоны из индекса: отбор по t неуязвим к сбоям
        # счётчика n на аномальных файлах; границы — точные метки кадров.
        vpts = frame_provider.get_visible_pts(ws.original)
        frame_ts_ranges = []
        for (start, end) in fragments:
            if not 0 <= start <= end < len(vpts):
                raise FFmpegError(
                    f"Фрагмент [{start}, {end}] вне диапазона "
                    f"[0, {len(vpts)})")
            frame_ts_ranges.append(_frame_ts_bounds(vpts, start, end))
        def progress(fragment_ind: int, _total: int, _fragment) -> None:
            with EXPORTS_LOCK:
                if ws_id in EXPORT_CANCEL:
                    raise _ExportCancelled()
                EXPORTS[ws_id] = {
                    "state": "running",
                    "index": fragment_ind,
                    "total": total,
                }

        def cancelled() -> bool:
            with EXPORTS_LOCK:
                return ws_id in EXPORT_CANCEL

        created = exporter.extract_fragments(
            fragments, progress=progress, cancelled=cancelled,
            frame_ts_ranges=frame_ts_ranges)
        for (start, end), path in zip(fragments, created):
            _verify_cut(ws.original, start, path, end - start + 1)
        vid = _video_identity(ws.original)
        task_hash = _task_hash(frags, vid["size"], vid["mtime_ns"]) \
            if vid is not None else None
        _export_finished_ok(ws_id, created, task_hash)
    except (_ExportCancelled, ExportCancelled):
        # Файлы, появившиеся за этот запуск (готовые фрагменты валидны,
        # но пользователь просил зачистку) — удаляем по снапшоту каталога.
        try:
            after = set(os.listdir(out_dir))
        except OSError:
            after = set()
        created_now = [os.path.join(out_dir, name) for name in after - before]
        _export_cancelled(ws_id, created_now)
    except FFmpegError as e:
        # Проваленный прогон не оставляет мусора: чистим созданное сейчас
        # (как при отмене), чтобы следующий запуск начинался с чистого поля.
        try:
            after = set(os.listdir(out_dir))
        except OSError:
            after = set()
        for name in after - before:
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass
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
    """Запускает ffmpeg-нарезку в фоне; прогресс — через status.

    Нарезки актуальны (хеш границ+видео совпал с готовыми) — сразу done
    с файлами, без прогона. ``?force=1`` — всегда гнать заново.
    """
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

    if request.GET.get("force") != "1":
        done = _current_done(pair_id)
        if done is not None:
            return JsonResponse(done)

    # Несоответствие — сносим прежние наши нарезки и запускаем новое.
    _drop_previous_outputs(ws)

    with EXPORTS_LOCK:
        EXPORTS[pair_id] = {"state": "running", "index": 0, "total": len(frags)}

    threading.Thread(target=_run_export, args=(pair_id,), daemon=True).start()
    return JsonResponse({"state": "running", "index": 0, "total": len(frags)})


@require_http_methods(["POST"])
def fragment_export_cancel(request, pair_id: str):
    """Просит фоновый экспорт остановиться (в т.ч. посреди фрагмента, ~0.5с).

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
    if state and state.get("state") != "done":
        # Бегущий/упавший/отменённый — как есть (память).
        if state["state"] == "error":
            return JsonResponse({"state": "error", "error": state.get("error", "Ошибка экспорта")})
        if state["state"] in ("cancelled", "cancelling"):
            return JsonResponse({"state": state["state"]})
        return JsonResponse({
            "state": "running",
            "index": state.get("index", 0),
            "total": state.get("total", 1),
        })
    # done или нет памяти: перепроверяем под текущее состояние (границы+видео
    # могли измениться после финиша; рестарт стирает память).
    done = _current_done(pair_id)
    if done is not None:
        return JsonResponse(done)
    return JsonResponse({"state": "idle"})


def _current_done(pair_id: str):
    """Готовый результат под ТЕКУЩЕЕ состояние (None — гнать заново)."""
    ws = get_workspace(pair_id)
    if ws is None:
        return None
    frags = ws.load_fragments()
    if not frags or not ws.original:
        return None
    vid = _video_identity(ws.original)
    if vid is None:
        return None
    want = _task_hash(frags, vid["size"], vid["mtime_ns"])
    with EXPORTS_LOCK:
        state = EXPORTS.get(pair_id)
    if state and state.get("state") == "done" and state.get("hash") == want:
        return {"state": "done", "files": state["files"]}
    return _status_from_disk(pair_id, want)


def _drop_previous_outputs(ws) -> None:
    """Удаляет прежние наши нарезки перед новым прогоном (mismatch/force).

    Только файлы из sidecar и нашего именного шаблона; чужие (keep.txt) целы.
    """
    out_dir = os.path.join(ws.path, "exports")
    victims = set()
    try:
        with open(os.path.join(out_dir, _SIDECAR), encoding="utf-8") as f:
            saved = json.load(f)
        for filename in saved.get("files") or []:
            victims.add(os.path.basename(filename))
    except (OSError, ValueError):
        pass
    try:
        for filename in os.listdir(out_dir):
            if "_fragment_" in filename:
                victims.add(filename)
    except OSError:
        return
    for filename in victims:
        try:
            os.remove(os.path.join(out_dir, filename))
        except OSError:
            pass


def _status_from_disk(pair_id: str, want: str):
    """Статус done из sidecar (после рестарта) или None.

    Возвращает done, только если sidecar-хеш совпал с нынешним состоянием
    и все файлы на месте и ненулевые.
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
    if not saved.get("hash") or saved["hash"] != want:
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
    return {"state": "done", "files": urls}


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