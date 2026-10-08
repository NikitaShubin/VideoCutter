# -*- coding: utf-8 -*-
"""Экспорт/импорт разметки задачи: архив backup_format (kind=annotations).

GET  pairs/<id>/annotations        — manifest.json + fragments.tsv
                                     (?media=1 — со всеми видео папки).
POST pairs/<id>/annotations/import — применяет к задаче ТОЛЬКО
                                     fragments.tsv: сверка отпечатка медиа
                                     (409, ?force=1 обходит), структурные
                                     проверки строгого парсера (диапазон
                                     кадров/пересечения — 400/409), затем
                                     атомарная замена файла. Битый архив
                                     отклоняется до записи — задача не
                                     может испортиться при импорте.

Формат архива — backup_format (чистый модуль), строгий парсер
fragments.tsv — workspace.parse_fragments_tsv_strict (рядом с _read),
временные файлы и отпечаток — общие хелперы vc_pairs.backup. Паспорты
задач/проектов модуль не трогает.
"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
import zlib
from typing import Optional

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

import backup_format as fmt
from vc_pairs.backup import (_task_display, file_response,
                             make_temp_zip_path, media_fingerprint,
                             unlink_quiet, video_members)
from vc_pairs.views import _storage_error, _ws_404
from workspace import FRAGMENTS_FILE, get_workspace, parse_fragments_tsv_strict

#: Заголовок пустой разметки (в архив, если у задачи ещё нет fragments.tsv).
_EMPTY_TSV = b"start\tend\tcomment\n"


@require_http_methods(["GET"])
def annotations_export(request, pair_id: str):
    """GET — архив разметки задачи; ``?media=1`` добавляет видео папки."""
    ws = get_workspace(pair_id)
    if ws is None:
        return _ws_404(pair_id)
    with_media = request.GET.get("media") in ("1", "true", "yes")

    path = make_temp_zip_path()
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Канонический слепок: без id экземпляра (см. backup_format 1.1).
            manifest = fmt.make_manifest(
                fmt.KIND_ANNOTATIONS, resource=fmt.RESOURCE_TASK,
                name=_task_display(ws.path, pair_id),
                media=media_fingerprint(ws))
            zf.writestr(fmt.MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=2))
            tsv = ws.fragments_path()
            if os.path.isfile(tsv):
                zf.write(tsv, FRAGMENTS_FILE, compress_type=zipfile.ZIP_DEFLATED)
            else:
                # Разметки ещё нет — пустая: roundtrip импорта работает.
                zf.writestr(FRAGMENTS_FILE, _EMPTY_TSV)
            if with_media:
                for name in video_members(ws.path):
                    zf.write(os.path.join(ws.path, name), name,
                             compress_type=zipfile.ZIP_STORED)
    except OSError as e:
        unlink_quiet(path)
        return _storage_error(e)
    except BaseException:
        unlink_quiet(path)
        raise
    return file_response(path, f"{pair_id}-annotations.zip")


@require_http_methods(["POST"])
def annotations_import(request, pair_id: str):
    """POST multipart ``file`` — применить архив разметки к задаче.

    Сверка отпечатка медиа по числу кадров (409; ``?force=1`` — без сверки),
    структурные проверки — как в PUT /fragments (диапазон 400, пересечения
    409). Успех — 200 и счётчики применённой разметки.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return _ws_404(pair_id)
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse(
            {"error": "Не передан файл архива (поле 'file')"}, status=400)
    force = request.GET.get("force") in ("1", "true", "yes")

    try:
        zf, names, manifest = fmt.open_archive(upload)
    except fmt.ArchiveError as e:
        return JsonResponse({"error": str(e)}, status=400)

    tmp: Optional[str] = None
    try:
        if manifest.get("kind") != fmt.KIND_ANNOTATIONS:
            return JsonResponse(
                {"error": "Ожидается архив разметки (kind=annotations); "
                          "полный бэкап — через POST /api/v1/backups/import"},
                status=400)
        if FRAGMENTS_FILE not in names:
            return JsonResponse(
                {"error": f"В архиве нет {FRAGMENTS_FILE} — нечего импортировать"},
                status=400)

        # Число кадров цели: и для сверки отпечатка, и для диапазонов —
        # тот же источник, что в PUT /fragments.
        total_frames = ws.metadata()["total_frames"]

        if not force:
            problem = fmt.fingerprint_problem(
                manifest.get("media"), {"total_frames": total_frames})
            if problem:
                return JsonResponse(
                    {"error": f"{problem}. Повторите с force=1, "
                              "если видео действительно то же"},
                    status=409)

        try:
            info = zf.getinfo(FRAGMENTS_FILE)
        except KeyError:
            return JsonResponse(
                {"error": f"В архиве нет {FRAGMENTS_FILE} — нечего импортировать"},
                status=400)
        if info.file_size > fmt.FRAGMENTS_MAX_BYTES:
            return JsonResponse(
                {"error": "Разметка в архиве подозрительно большая"}, status=400)
        try:
            data = zf.read(FRAGMENTS_FILE)
        except (zipfile.BadZipFile, zlib.error, EOFError) as e:
            return JsonResponse(
                {"error": f"Архив повреждён: {e}"}, status=400)

        frags, position, problem = parse_fragments_tsv_strict(data, total_frames)
        if problem is not None:
            status, message = problem
            return JsonResponse({"error": message}, status=status)

        # Атомарная замена: записали во временный файл рядом с целью,
        # rename — одним шагом (читатели не видят полузаписанное).
        try:
            fd, tmp = tempfile.mkstemp(prefix=".fragments-import-",
                                       dir=ws.path)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, ws.fragments_path())
            tmp = None
        except OSError as e:
            return _storage_error(e)
        ws.invalidate_cache()
        return JsonResponse(
            {"task_id": pair_id, "fragments": len(frags), "position": position})
    finally:
        zf.close()
        unlink_quiet(tmp)
