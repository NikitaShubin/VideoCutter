# -*- coding: utf-8 -*-
import os

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
    """Запускает ffmpeg-нарезку оригиналов по всем фрагментам пары.

    Результат сохраняется на сервере в VC_EXPORT_ROOT/{pair_id}/.
    Возвращает список файлов с относительными URL для скачивания.
    """
    pair = get_object_or_404(VideoPair, pk=pair_id)
    qs = Fragment.objects.filter(video_pair=pair).order_by("start")
    if not qs.exists():
        return Response({"error": "Нет фрагментов для экспорта"}, status=status.HTTP_400_BAD_REQUEST)

    out_dir = os.path.join(settings.VC_EXPORT_ROOT, str(pair.id))
    os.makedirs(out_dir, exist_ok=True)

    exporter = Exporter(pair.original.path, out_dir)
    fragments = [(f.start, f.end) for f in qs]
    try:
        created = exporter.extract_fragments(fragments)
    except FFmpegError as e:
        return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    urls = []
    for i, path in enumerate(created, 1):
        filename = os.path.basename(path)
        urls.append(
            {
                "index": i,
                "filename": filename,
                "url": f"/api/v1/pairs/{pair.id}/export/{filename}",
            }
        )
    return Response({"files": urls})


@api_view(["GET"])
def fragment_export_download(request, pair_id: int, path: str):
    """Скачивает экспортированный фрагмент (файлы уже сгенерированы)."""
    pair = get_object_or_404(VideoPair, pk=pair_id)
    base = os.path.abspath(settings.VC_EXPORT_ROOT)
    full = os.path.abspath(os.path.join(base, str(pair.id), path))
    if not full.startswith(os.path.join(base, str(pair.id))) or not os.path.isfile(full):
        raise Http404("Файл не найден")
    return FileResponse(open(full, "rb"), as_attachment=True, filename=os.path.basename(full))