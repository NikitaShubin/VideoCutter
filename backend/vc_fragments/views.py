# -*- coding: utf-8 -*-
import os
import threading

from django.conf import settings
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from videocutter.core.exporter import Exporter, FFmpegError

from vc_pairs.models import VideoPair
from .models import Fragment
from .serializers import FragmentSerializer

# Прогресс экспортов в памяти процесса: pair_id -> {"state": ..., ...}.
# Подходит для dev-сервера / одного worker-а; поток обновляет dict.
EXPORTS: dict[int, dict] = {}
EXPORTS_LOCK = threading.Lock()


def _export_finished_ok(pair_id: int, created: list[str]) -> None:
    urls = []
    for i, path in enumerate(created, 1):
        filename = os.path.basename(path)
        urls.append(
            {
                "index": i,
                "filename": filename,
                "url": f"/api/v1/pairs/{pair_id}/export/{filename}",
            }
        )
    with EXPORTS_LOCK:
        EXPORTS[pair_id] = {"state": "done", "files": urls}


def _export_failed(pair_id: int, message: str) -> None:
    with EXPORTS_LOCK:
        EXPORTS[pair_id] = {"state": "error", "error": message}


def _run_export(pair_id: int) -> None:
    """Выполняет нарезку фрагментов в фоне, обновляя прогресс в EXPORTS."""
    try:
        pair = get_object_or_404(VideoPair, pk=pair_id)
        qs = Fragment.objects.filter(video_pair=pair).order_by("start")
        out_dir = os.path.join(settings.VC_EXPORT_ROOT, str(pair.id))
        os.makedirs(out_dir, exist_ok=True)

        exporter = Exporter(pair.original.path, out_dir)
        fragments = [(f.start, f.end) for f in qs]
        total = len(fragments)

        def progress(fragment_ind: int, _total: int, _fragment) -> None:
            with EXPORTS_LOCK:
                EXPORTS[pair_id] = {
                    "state": "running",
                    "index": fragment_ind,
                    "total": total,
                }

        created = exporter.extract_fragments(fragments, progress=progress)
        _export_finished_ok(pair_id, created)
    except FFmpegError as e:
        _export_failed(pair_id, str(e))
    except Exception as e:  # localStorage/файловые ошибки — текст клиенту
        _export_failed(pair_id, str(e))


@api_view(["GET", "POST"])
def fragment_list(request, pair_id: int):
    """GET — список фрагментов пары; POST — добавить один фрагмент (body{start,end})."""
    pair = get_object_or_404(VideoPair, pk=pair_id)
    if request.method == "GET":
        qs = Fragment.objects.filter(video_pair=pair).order_by("start")
        return Response(FragmentSerializer(qs, many=True).data)

    data = {
        "video_pair": pair_id,
        "start": request.data.get("start"),
        "end": request.data.get("end"),
        "comment": request.data.get("comment", ""),
    }
    ser = FragmentSerializer(data=data)
    ser.is_valid(raise_exception=True)

    # Защита от пересечений и выходов за границы кадров.
    if not (0 <= data["start"] <= data["end"] < pair.total_frames):
        return Response(
            {"error": f"Фрагмент вне диапазона кадров [0, {pair.total_frames})"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    existing = Fragment.objects.filter(video_pair=pair)
    for f in existing:
        if not (data["end"] < f.start or data["start"] > f.end):
            return Response(
                {"error": f"Фрагмент пересекается с уже существующим [{f.start}, {f.end}]"},
                status=status.HTTP_409_CONFLICT,
            )

    frag = Fragment.objects.create(
        video_pair=pair,
        start=data["start"],
        end=data["end"],
        comment=data["comment"],
    )
    return Response(FragmentSerializer(frag).data, status=status.HTTP_201_CREATED)


@api_view(["PUT"])
def fragment_replace(request, pair_id: int):
    """Заменяет весь список фрагментов пары на переданный (body: list of {start,end})."""
    pair = get_object_or_404(VideoPair, pk=pair_id)
    items = request.data
    if not isinstance(items, list):
        return Response({"error": "Ожидался список фрагментов"}, status=status.HTTP_400_BAD_REQUEST)

    clean = []
    old_comments = {
        (f.start, f.end): f.comment
        for f in Fragment.objects.filter(video_pair=pair)
    }
    for item in items:
        try:
            start, end = int(item["start"]), int(item["end"])
        except (KeyError, TypeError, ValueError):
            return Response(
                {"error": f"Неверный фрагмент: {item}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if start < 0 or end >= pair.total_frames or start > end:
            return Response(
                {"error": f"Фрагмент {start}-{end} вне диапазона [0, {pair.total_frames})"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if isinstance(item, dict) and "comment" in item:
            comment = item["comment"]
        else:
            comment = old_comments.get((start, end), "")
        clean.append((start, end, comment or ""))

    # Проверка пересечений после сортировки.
    clean.sort(key=lambda x: (x[0], x[1]))
    for i in range(1, len(clean)):
        if clean[i][0] <= clean[i - 1][1]:
            return Response(
                {"error": "Фрагменты пересекаются"},
                status=status.HTTP_409_CONFLICT,
            )

    Fragment.objects.filter(video_pair=pair).delete()
    Fragment.objects.bulk_create(
        [
            Fragment(video_pair=pair, start=s, end=e, comment=c)
            for s, e, c in clean
        ]
    )
    return Response(FragmentSerializer(Fragment.objects.filter(video_pair=pair), many=True).data)


@api_view(["DELETE"])
def fragment_delete(request, pair_id: int, frag_id: int):
    frag = get_object_or_404(Fragment, pk=frag_id, video_pair_id=pair_id)
    frag.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["POST"])
def fragment_export(request, pair_id: int):
    """Запускает ffmpeg-нарезку оригиналов в фоне; прогресс — через status.

    Возвращает {"state":"running","index":0,"total":N}. Результат
    запрашивается GET /export/status/ (файлы + состояние done/error).
    """
    pair = get_object_or_404(VideoPair, pk=pair_id)
    qs = Fragment.objects.filter(video_pair=pair).order_by("start")
    if not qs.exists():
        return Response({"error": "Нет фрагментов для экспорта"}, status=status.HTTP_400_BAD_REQUEST)

    with EXPORTS_LOCK:
        current = EXPORTS.get(pair_id)
        if current and current["state"] == "running":
            return Response(
                {"error": "Экспорт уже выполняется для этой пары"},
                status=status.HTTP_409_CONFLICT,
            )
        EXPORTS[pair_id] = {"state": "running", "index": 0, "total": len(qs)}

    threading.Thread(target=_run_export, args=(pair_id,), daemon=True).start()
    return Response({"state": "running", "index": 0, "total": len(qs)})


@api_view(["GET"])
def fragment_export_status(request, pair_id: int):
    """Текущий статус экспорта пары: running (index/total) или done/error."""
    with EXPORTS_LOCK:
        state = EXPORTS.get(pair_id)
    if not state:
        return Response({"state": "idle"})
    if "files" in state:
        return Response({"state": "done", "files": state["files"]})
    if state["state"] == "error":
        return Response({"state": "error", "error": state.get("error", "Ошибка экспорта")})
    return Response({
        "state": "running",
        "index": state.get("index", 0),
        "total": state.get("total", 1),
    })


@api_view(["GET"])
def fragment_export_download(request, pair_id: int, path: str):
    """Скачивает экспортированный фрагмент (файлы уже сгенерированы)."""
    pair = get_object_or_404(VideoPair, pk=pair_id)
    base = os.path.abspath(settings.VC_EXPORT_ROOT)
    full = os.path.abspath(os.path.join(base, str(pair.id), path))
    if not full.startswith(os.path.join(base, str(pair.id))) or not os.path.isfile(full):
        raise Http404("Файл не найден")
    return FileResponse(open(full, "rb"), as_attachment=True, filename=os.path.basename(full))