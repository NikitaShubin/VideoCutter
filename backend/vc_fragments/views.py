# -*- coding: utf-8 -*-
"""Fragment views: TSV-based (никакой БД, никакого DRF).

Фрагменты хранятся как fragments.tsv в директории workspace.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from typing import Optional

import numpy as np
from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_GET, require_http_methods

import export_access
from export_guard import START_LOCK
from export_ledger import LEDGER
from videocutter.core.exporter import (
    Exporter,
    ExportCancelled,
    FFmpegError,
)
from vc_pairs import frame_provider
import workspace
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


# Совместимость с прежним interface: те же объекты, но владеет ими
# ExportLedger. Новый код ходит через LEDGER; алиасы оставлены для тестов
# и внешних импортов.
EXPORTS = LEDGER.states
EXPORTS_LOCK = LEDGER.lock
EXPORT_CANCEL = LEDGER.cancel_set


# --- очередь экспортов (CVAT-style, in-process, без Redis) ---
def _export_capacity() -> int:
    """Параллельных экспортов: автотюн от CPU + VC_EXPORT_WORKERS-оверрайд.

    Work-conserving: слот свободен — старт сразу (параллель при idle),
    нет — FIFO-очередь. Один экспорт ~3-4 ядра (libx264 slow 1080p).
    Читается динамически (тесты + будущий runtime).
    """
    raw = os.environ.get("VC_EXPORT_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, min(4, (os.cpu_count() or 4) // 4))


# FIFO workspace_id ожидающих. Владеет очередью ExportLedger; алиас
# оставлен для тестов и внешних импортов.
_EXPORT_QUEUE = LEDGER.queue


def _run_export_guarded(ws_id: str) -> None:
    """Обёртка потока экспорта: любой исход освобождает слот следующему."""
    try:
        _run_export(ws_id)
    finally:
        _promote_exports()


def _spawn_export(ws_id: str) -> None:
    threading.Thread(target=_run_export_guarded, args=(ws_id,),
                     daemon=True).start()


def _promote_exports() -> None:
    """Занять освободившиеся слоты первыми из очереди.

    Решение и пометка — внутри ExportLedger; spawn потоков снаружи, чтобы
    не держать lock на создании потоков.
    """
    for ws_id in LEDGER.promote(_export_capacity()):
        _spawn_export(ws_id)


class _ExportCancelled(Exception):
    """Флаг отмены: экспорт остановлен пользователем между фрагментами."""


# Sidecar готового экспорта (рядом с файлами): переживает рестарт процесса.
# Без него повторное открытие задачи после рестарта видело бы idle и гнало
# экспорт заново, хотя файлы на месте.
_SIDECAR = LEDGER.sidecar_name


def _video_identity(path: str):
    """Идентичность видео {size, mtime_ns} (None — файла нет)."""
    return LEDGER.video_identity(path)


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
    """Хеш состояния нарезки: границы (порядок не важен) + видео."""
    return LEDGER.task_hash(frags, size, mtime_ns)


def _export_finished_ok(ws_id: str, created: list[str], task_hash) -> None:
    """Совместимость: фиксация готового экспорта живёт в ExportLedger."""
    LEDGER.finish(ws_id, created, task_hash)


def _export_failed(ws_id: str, message: str) -> None:
    LEDGER.fail(ws_id, message)


def _export_cancelled(ws_id: str, created_now: list[str]) -> None:
    """Отмена пользователем: зачищаем частичные файлы этого запуска."""
    LEDGER.cancel(ws_id, created_now)


_THUMB_W = 64
_THUMB_H = 36
_THUMB_BYTES = _THUMB_W * _THUMB_H


def _decode_timeout(frames_wanted: int) -> float:
    """Таймаут декода: запас на кадр + база, но не меньше 1500 с.

    Фиксированные 1500 с валят длинные нарезки (100k кадров декодируются
    далеко за этот предел), при этом зависший декодер всё равно ограничен.
    """
    return max(1500.0, float(frames_wanted) / 5.0 + 300.0)


def _thumb_cmd(path: str, frames_wanted: int) -> list:
    """Единый пайплайн миниатюр: passthrough-кадры, без seek'ов.

    ``-vsync 0`` обязателен: без него ffmpeg дублирует кадры VFR до CFR
    (90 -> 119 на смене темпа), и ординалы перестают быть ординалами.
    """
    return [
        "ffmpeg", "-v", "error", "-i", path,
        "-frames:v", str(max(0, frames_wanted)),
        "-vsync", "0",
        "-vf", f"scale={_THUMB_W}:{_THUMB_H},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]


def _thumb_at(raw: bytes, index: int) -> np.ndarray:
    start = index * _THUMB_BYTES
    return np.frombuffer(raw[start:start + _THUMB_BYTES],
                         dtype=np.uint8).astype(np.float32)


def _decode_thumbs(path: str, frames_wanted: int,
                   timeout: Optional[float] = None) -> Optional[list]:
    """Секвентальный декод в gray-миниатюры 64x36 (без seek'ов и счётчиков).

    Seek'и недостоверны на части контейнеров (MPEG-PS: seek приземляется
    на GOP позже даже у ffmpeg CLI; ASF: чтение после seek падает в cv2),
    а cv2-сборка не умеет часть кодеков (AV1). Единственный честный путь —
    декодить с начала и брать нужные ординалы. Миниатюры режут и трафик
    пайпа (2.3 КБ/кадр), и чувствительность к зерну пересжатия.
    Возвращает None при обрыве декода.
    """
    if timeout is None:
        timeout = _decode_timeout(frames_wanted)
    try:
        proc = subprocess.run(_thumb_cmd(path, frames_wanted),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout
    n = len(raw) // _THUMB_BYTES
    if n * _THUMB_BYTES != len(raw):
        return None
    return [_thumb_at(raw, i) for i in range(n)]


# Окно поиска тихого сдвига (в кадрах по обе стороны): покрывает
# наблюдавшиеся +2 (start_time) с запасом; большие уезды всё равно
# валятся номинальным порогом.
_VERIFY_SHIFT_WINDOW = 3


def _require_cut_count(got_count: Optional[int], expected_count: int,
                       cut_path: str) -> None:
    """Ровно expected_count кадров в нарезке (лишний кадр — тоже дефект)."""
    if got_count is None:
        raise FFmpegError(f"Нарезка не декодируется целиком ({cut_path})")
    if got_count < expected_count:
        raise FFmpegError(
            f"Кадров в нарезке: {got_count}, ожидалось: {expected_count} "
            f"({cut_path})")
    if got_count > expected_count:
        raise FFmpegError(
            f"Кадров в нарезке больше ожидаемого: {got_count} > "
            f"{expected_count} ({cut_path})")


def _compare_cut(exp: list, got: list, cut_path: str) -> None:
    """КАЖДЫЙ кадр нарезки против точного ординала + детект тихого сдвига.

    Порог VERIFY_SSD_MAX отделяет совпадения (единицы) от поломок
    (сотни–тысячи) с запасом: истинное совмещение даёт ssd≈0 даже после
    CRF-пересжатия, так что превышение — само по себе дефект (не тот
    диапазон, мусор, битый транскод). Дополнительно ловится тихий сдвиг:
    если соседний эталон совпал на порядок лучше номинального — границы
    уехали, хотя счётчик сошёлся (статику ±1 не различить в принципе —
    там всё одинаково, включая соседей).
    """
    expected_count = len(got)
    for i in range(expected_count):
        d = exp[i] - got[i]
        nom = float((d * d).mean())
        if i == 0:
            what = "первого"
        elif i == expected_count - 1:
            what = "последнего"
        else:
            what = f"кадра {i + 1}/{expected_count}"
        if nom > 1.0:
            # Тихий сдвиг проверяем ДО порога: соседний эталон совпал
            # на порядок лучше — вырезано не то, хотя счётчик сошёлся.
            # У одиночного кадра соседей в нарезке нет: сдвиг ловить нечем,
            # остаётся порог контента (регресс min() на пустом диапазоне).
            lo = max(0, i - _VERIFY_SHIFT_WINDOW)
            hi = min(expected_count, i + _VERIFY_SHIFT_WINDOW + 1)
            neighbours = [j for j in range(lo, hi) if j != i]
            best = min(float(((exp[j] - got[i]) ** 2).mean())
                       for j in neighbours) if neighbours else None
            if best is not None and best <= nom / 10:
                raise FFmpegError(
                    f"Похоже на сдвиг границ: {what} кадр нарезки совпал "
                    f"с соседним эталоном (ssd={best:.1f}), а не со своим "
                    f"(ssd={nom:.1f}): {cut_path}")
        if nom > VERIFY_SSD_MAX:
            raise FFmpegError(
                f"Контент {what} кадра не совпал "
                f"(ssd={nom:.1f} > {VERIFY_SSD_MAX}): {cut_path}")


def _verify_cut(src_path: str, start_index: int, cut_path: str,
                expected_count: int) -> None:
    """Полная проверка одной нарезки: точный счёт + КАЖДЫЙ кадр + сдвиг.

    Счётчик нарезки берётся из её же декода: просим expected_count+1 кадр.
    Ровно expected_count — норма; меньше — потерян кадр, больше — лишний
    (декодер остановлен на expected_count+1, отдельный полный подсчёт не
    нужен). Эталон — те же ординалы исходника секвентальным декодом.

    :raises FFmpegError: несовпадение — молчаливого успеха не будет.
    """
    got = _decode_thumbs(cut_path, expected_count + 1)
    _require_cut_count(
        None if got is None else len(got), expected_count, cut_path)
    exp_all = _decode_thumbs(src_path, start_index + expected_count)
    if exp_all is None or len(exp_all) < start_index + expected_count:
        raise FFmpegError(
            f"Не декодируются эталоны 0..{start_index + expected_count - 1} "
            f"из {src_path}")
    exp = exp_all[start_index:start_index + expected_count]
    _compare_cut(exp, got, cut_path)


def _raw_frame_count(raw_path: str) -> int:
    return os.path.getsize(raw_path) // _THUMB_BYTES


def _read_thumb_at(raw_file, index: int, path: str) -> np.ndarray:
    """Миниатюра № index из raw-файла прохода.

    Читается временный файл (не исходник): seek по нему не влияет на
    ординалы — они уже зафиксированы секвентальным декодом.
    """
    raw_file.seek(index * _THUMB_BYTES)
    raw = raw_file.read(_THUMB_BYTES)
    if len(raw) != _THUMB_BYTES:
        raise FFmpegError(
            f"Не декодируются кадры до {index} включительно: {path}")
    return _thumb_at(raw, 0)


def _decode_to_raw(path: str, frames_wanted: int, raw_path: str,
                   cancelled=None, what: str = "эталонов") -> None:
    """Один секвентальный проход файла в raw-файл миниатюр.

    stdout пишется в файл: poll-цикл не блокируется на пайпе и может
    проверять отмену и общий таймаут. Короткий хвост (битый исходник) —
    не глобальная ошибка: сколько кадров декодировалось, столько доступно,
    а нужность конкретных ординалов решает сверка фрагмента.
    """
    timeout = _decode_timeout(frames_wanted)
    try:
        with open(raw_path, "wb") as out:
            proc = subprocess.Popen(
                _thumb_cmd(path, frames_wanted),
                stdout=out, stderr=subprocess.DEVNULL)
            try:
                if cancelled is not None and cancelled():
                    raise ExportCancelled(
                        f"Экспорт отменён при сверке {what}: {path}")
                deadline = time.monotonic() + timeout
                while proc.poll() is None:
                    if cancelled is not None and cancelled():
                        raise ExportCancelled(
                            f"Экспорт отменён при сверке {what}: {path}")
                    if time.monotonic() > deadline:
                        raise FFmpegError(
                            f"Таймаут декода {what} ({timeout:.0f} с): "
                            f"{path}")
                    time.sleep(0.1)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
    except OSError as exc:
        raise FFmpegError(
            f"Не удалось декодировать {what} из {path}: {exc}")


def _compare_cut_raws(src_raw, start_index: int, src_available: int,
                      cut_raw, expected_count: int,
                      src_path: str, cut_path: str) -> None:
    """Сверка нарезки с эталонами из raw-файлов, без списков в памяти.

    Память — O(1) на кадр (плюс окно ±3 для детекта сдвига): 100k-кадровая
    нарезка не превращается в ~1 ГБ float32.
    """
    if start_index + expected_count > src_available:
        raise FFmpegError(
            f"Не декодируются эталоны {start_index}.."
            f"{start_index + expected_count - 1} из {src_path} "
            f"(декодировано кадров: {src_available})")
    for i in range(expected_count):
        got_i = _read_thumb_at(cut_raw, i, cut_path)
        exp_i = _read_thumb_at(src_raw, start_index + i, src_path)
        d = exp_i - got_i
        nom = float((d * d).mean())
        if i == 0:
            what = "первого"
        elif i == expected_count - 1:
            what = "последнего"
        else:
            what = f"кадра {i + 1}/{expected_count}"
        if nom > 1.0:
            # Тихий сдвиг проверяем ДО порога: соседний эталон совпал
            # на порядок лучше — вырезано не то, хотя счётчик сошёлся.
            # У одиночного кадра соседей в нарезке нет: сдвиг ловить нечем,
            # остаётся порог контента (регресс min() на пустом диапазоне).
            lo = max(0, i - _VERIFY_SHIFT_WINDOW)
            hi = min(expected_count, i + _VERIFY_SHIFT_WINDOW + 1)
            neighbours = [j for j in range(lo, hi) if j != i]
            best = min(
                float(((_read_thumb_at(
                    src_raw, start_index + j, src_path) - got_i) ** 2).mean())
                for j in neighbours) if neighbours else None
            if best is not None and best <= nom / 10:
                raise FFmpegError(
                    f"Похоже на сдвиг границ: {what} кадр нарезки совпал "
                    f"с соседним эталоном (ssd={best:.1f}), а не со своим "
                    f"(ssd={nom:.1f}): {cut_path}")
        if nom > VERIFY_SSD_MAX:
            raise FFmpegError(
                f"Контент {what} кадра не совпал "
                f"(ssd={nom:.1f} > {VERIFY_SSD_MAX}): {cut_path}")


def _verify_export_cuts(src_path: str, items, cancelled=None) -> None:
    """Сверка всех нарезок прогона: один проход исходника от кадра 0.

    ``items`` — (start, end, cut_path). Фрагменты не пересекаются и
    проверяются по возрастанию start: исходник декодируется секвентально
    до максимального end без seek'ов, поэтому ординалы точны. И исходник,
    и каждая нарезка декодируются в raw-файлы на диске и сверяются
    потоково — память не зависит от длины фрагмента. Пересечение
    (аномалия) — независимая сверка каждого: общий проход для
    пересекающихся диапазонов не определён.
    """
    if not items:
        return
    ordered = sorted(items, key=lambda item: item[0])
    if any(ordered[i][0] <= ordered[i - 1][1]
           for i in range(1, len(ordered))):
        for start, end, cut_path in ordered:
            _verify_cut(src_path, start, cut_path, end - start + 1)
        return

    frames_wanted = max(end for _, end, _ in ordered) + 1
    fd, raw_path = tempfile.mkstemp(prefix="vc-verify-", suffix=".raw")
    os.close(fd)
    fd, cut_raw_path = tempfile.mkstemp(
        prefix="vc-verify-cut-", suffix=".raw")
    os.close(fd)
    try:
        _decode_to_raw(
            src_path, frames_wanted, raw_path, cancelled=cancelled)
        available = _raw_frame_count(raw_path)
        with open(raw_path, "rb") as raw_file:
            for start, end, cut_path in ordered:
                expected_count = end - start + 1
                _decode_to_raw(
                    cut_path, expected_count + 1, cut_raw_path,
                    cancelled=cancelled, what="нарезки")
                cut_count = _raw_frame_count(cut_raw_path)
                _require_cut_count(cut_count, expected_count, cut_path)
                # Читатель открывается заново на каждую нарезку: файл
                # перезаписан, и буфер прежнего дескриптора мог бы отдать
                # миниатюры предыдущего фрагмента.
                with open(cut_raw_path, "rb") as cut_raw:
                    _compare_cut_raws(
                        raw_file, start, available, cut_raw,
                        expected_count, src_path, cut_path)
    finally:
        for path in (raw_path, cut_raw_path):
            try:
                os.unlink(path)
            except OSError:
                pass



def _remove_created_since(out_dir: str, before: set) -> None:
    """Удаляет файлы, появившиеся в out_dir с момента снапшота before."""
    if not out_dir:
        return
    try:
        after = set(os.listdir(out_dir))
    except OSError:
        return
    for name in after - before:
        try:
            os.remove(os.path.join(out_dir, name))
        except OSError:
            pass


class _LedgerHooks:
    """Прежнее поведение: состояние экспорта живёт в ExportLedger."""

    def progress(self, ws_id: str, index: int, total: int) -> bool:
        return LEDGER.progress(ws_id, index, total)

    def cancelled(self, ws_id: str) -> bool:
        return LEDGER.cancelled(ws_id)

    def finished(self, ws_id: str, files: list[str], task_hash) -> None:
        _export_finished_ok(ws_id, files, task_hash)

    def failed(self, ws_id: str, message: str) -> None:
        _export_failed(ws_id, message)

    def cancelled_final(self, ws_id: str, created: list[str]) -> None:
        _export_cancelled(ws_id, created)

    def clear(self, ws_id: str) -> None:
        LEDGER.clear_cancel(ws_id)


_LEDGER_HOOKS = _LedgerHooks()


def _run_export(ws_id: str, hooks=None) -> None:
    """Выполняет нарезку фрагментов в фоне.

    ``hooks`` — точка подмены: планировщик фоновых операций передаёт свои
    прогресс/отмену/результат, а не пишет в ExportLedger. По умолчанию
    поведение прежнее: состояние ведёт ExportLedger.
    """
    hooks = hooks or _LEDGER_HOOKS
    out_dir = ""
    before = set()
    try:
        ws = get_workspace(ws_id)
        if ws is None:
            hooks.failed(ws_id, f"Workspace '{ws_id}' не найден")
            return

        frags = ws.load_fragments()
        if not frags:
            hooks.failed(ws_id, "Нет фрагментов для экспорта")
            return

        out_dir = os.path.join(ws.path, "exports")
        # Запас места проверяется до создания каталога и файлов: экспорт
        # не должен забивать диск под ноль. Внутри — перед каждым фрагментом.
        from vc_jobs import disk

        disk.ensure_free(ws.path)
        export_access.ensure_access(
            ws.path,
            root=os.environ.get("VC_ACCESS_ANCHOR")
            or workspace.WORKSPACE_ROOT)
        before = set(os.listdir(out_dir))

        exporter = Exporter(ws.original, out_dir, nice=EXPORT_NICE)
        fragments = [(f["start"], f["end"]) for f in frags]
        total = len(fragments)

        # Секундные диапазоны из индекса: отбор по t неуязвим к сбоям
        # счётчика n на аномальных файлах; границы — точные метки кадров.
        # Исключение — файлы без PTS у пакетов (AVI+B-кадры): метки там
        # гадаются каждым декодером по-своему (системный ±1), зато порядок
        # выдачи единодушен — отбираем по счётчику n (ординалы UI).
        vpts = frame_provider.get_visible_pts(ws.original)
        by_count = frame_provider.index_pts_unreliable(ws.original)
        frame_ts_ranges = []
        if not by_count:
            for (start, end) in fragments:
                if not 0 <= start <= end < len(vpts):
                    raise FFmpegError(
                        f"Фрагмент [{start}, {end}] вне диапазона "
                        f"[0, {len(vpts)})")
                frame_ts_ranges.append(_frame_ts_bounds(vpts, start, end))
        else:
            for (start, end) in fragments:
                if not 0 <= start <= end < len(vpts):
                    raise FFmpegError(
                        f"Фрагмент [{start}, {end}] вне диапазона "
                        f"[0, {len(vpts)})")
            frame_ts_ranges = None
        def progress(fragment_ind: int, _total: int, _fragment) -> None:
            disk.ensure_free(out_dir)
            if not hooks.progress(ws_id, fragment_ind, total):
                raise _ExportCancelled()

        def cancelled() -> bool:
            return hooks.cancelled(ws_id)

        created = exporter.extract_fragments(
            fragments, progress=progress, cancelled=cancelled,
            frame_ts_ranges=frame_ts_ranges)
        items = [(start, end, path)
                 for (start, end), path in zip(fragments, created)]
        _verify_export_cuts(ws.original, items, cancelled=cancelled)
        published = [exporter.publish(path) for _, _, path in items]
        vid = _video_identity(ws.original)
        task_hash = _task_hash(frags, vid["size"], vid["mtime_ns"]) \
            if vid is not None else None
        hooks.finished(ws_id, published, task_hash)
    except (_ExportCancelled, ExportCancelled):
        # Файлы, появившиеся за этот запуск (готовые фрагменты валидны,
        # но пользователь просил зачистку) — удаляем по снапшоту каталога.
        try:
            after = set(os.listdir(out_dir))
        except OSError:
            after = set()
        created_now = [os.path.join(out_dir, name) for name in after - before]
        hooks.cancelled_final(ws_id, created_now)
    except FFmpegError as e:
        # Проваленный прогон не оставляет мусора: чистим созданное сейчас
        # (как при отмене), чтобы следующий запуск начинался с чистого поля.
        _remove_created_since(out_dir, before)
        hooks.failed(ws_id, str(e))
    except Exception as e:
        # Любой сбой (не только FFmpegError) обязан зачистить частичные файлы.
        _remove_created_since(out_dir, before)
        hooks.failed(ws_id, str(e))
    finally:
        hooks.clear(ws_id)


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


def _active_export_job(pair_id: str):
    """Активная фоновая операция экспорта этой задачи (None — нет).

    Импорт ленивый: ``vc_jobs`` — обвязка, и старый путь экспорта не должен
    зависеть от её инициализации при импорте модуля.
    """
    import vc_jobs
    from vc_jobs.builtin_export import EXPORT_TYPE

    jobs = vc_jobs.active_jobs(type=EXPORT_TYPE, target=f"task:{pair_id}")
    return jobs[0] if jobs else None


@require_http_methods(["POST", "DELETE"])
def fragment_export(request, pair_id: str):
    """POST — запуск нарезки; DELETE — корзина: удалить все наши нарезки.

    POST: нарезки актуальны (хеш границ+видео совпал с готовыми) — сразу
    done с файлами, без прогона. ``?force=1`` — всегда гнать заново.
    Слоты ограничены (_export_capacity): занято — заявка встаёт в FIFO
    (state queued + position), слот освобождается финишем любого экспорта.
    Повторный POST при running/queued — 409 (кроме force: старую заявку
    снимаем — бегущей шлём cancel, queued выкидываем — и встаём заново).

    DELETE допустим только в спокойном состоянии (idle/done/error/cancelled):
    running/queued — 409, иначе поток писал бы в удаляемые файлы. Чужие файлы
    в exports/ не трогаются; sidecar забывается вместе с памятью.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    if request.method == "DELETE":
        with START_LOCK:
            active = _active_export_job(pair_id)
            if active is not None:
                return JsonResponse(
                    {"error": "Экспорт уже выполняется фоновой операцией "
                              f"{active.id} (state: {active.status})"},
                    status=409,
                )
            current = LEDGER.snapshot(pair_id)
            if current and current["state"] in ("running", "queued"):
                return JsonResponse(
                    {"error": "Экспорт уже выполняется/ожидает для этого workspace "
                              "(state: %s)" % current["state"]},
                    status=409,
                )
            LEDGER.purge_outputs(ws, remove=os.remove)
            LEDGER.forget(pair_id)
        return JsonResponse({"state": "idle"})

    frags = ws.load_fragments()
    if not frags:
        return JsonResponse({"error": "Нет фрагментов для экспорта"}, status=400)

    force = request.GET.get("force") == "1"
    with START_LOCK:
        active = _active_export_job(pair_id)
        if active is not None:
            return JsonResponse(
                {"error": "Экспорт уже выполняется фоновой операцией "
                          f"{active.id} (state: {active.status})"},
                status=409,
            )
        current = LEDGER.snapshot(pair_id)
        if current and current["state"] in ("running", "queued"):
            if not force:
                return JsonResponse(
                    {"error": "Экспорт уже выполняется/ожидает для этого "
                              "workspace (state: %s)" % current["state"]},
                    status=409,
                )
            # Force поверх активной заявки: бегущей — cancel, queued —
            # из очереди; ниже встанет свежая (сериализация на workspace:
            # два потока один ws никогда не режут параллельно).
            if current["state"] == "running":
                LEDGER.request_cancel(pair_id)
            else:
                LEDGER.forget(pair_id)

        if not force:
            done = _current_done(pair_id)
            if done is not None:
                return JsonResponse(done)

        # Несоответствие — сносим прежние наши нарезки и запускаем новое.
        _drop_previous_outputs(ws)

        body = LEDGER.submit(pair_id, len(frags), _export_capacity())
        if body["state"] == "running":
            _spawn_export(pair_id)
        return JsonResponse(body)


@require_http_methods(["POST"])
def fragment_export_cancel(request, pair_id: str):
    """Просит фоновый экспорт остановиться (в т.ч. посреди фрагмента, ~0.5с).

    Частичные файлы этого запуска зачищаются, состояние — "cancelled".
    Queued-заявка снимается с очереди сразу (тоже "cancelled").
    Отмена не бегущего/не ждущего экспорта — 409.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return JsonResponse({"error": f"Workspace '{pair_id}' не найден"}, status=404)

    current = LEDGER.snapshot(pair_id)
    if current and current["state"] == "queued":
        LEDGER.cancel(pair_id, [])
        return JsonResponse({"state": "cancelled"})
    if not current or current["state"] != "running":
        return JsonResponse(
            {"error": "Экспорт не выполняется для этого workspace"},
            status=409,
        )
    LEDGER.request_cancel(pair_id)
    return JsonResponse({"state": "cancelling"})


@require_GET
def fragment_export_status(request, pair_id: str):
    """Текущий статус экспорта."""
    state = LEDGER.snapshot(pair_id)
    if state and state.get("state") != "done":
        # Бегущий/упавший/отменённый/ждущий — как есть (память).
        if state["state"] == "error":
            return JsonResponse({"state": "error", "error": state.get("error", "Ошибка экспорта")})
        if state["state"] in ("cancelled", "cancelling"):
            return JsonResponse({"state": state["state"]})
        if state["state"] == "queued":
            return JsonResponse({
                "state": "queued",
                "position": state.get("position", 1),
                "total": state.get("total", 1),
            })
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
    return LEDGER.current_done(
        ws, video_identity=_video_identity, task_hash=_task_hash)


def _drop_previous_outputs(ws) -> None:
    """Удаляет прежние наши нарезки перед новым прогоном (mismatch/force).

    Только файлы из sidecar и нашего именного шаблона; чужие (keep.txt) целы.
    """
    LEDGER.drop_outputs(ws, remove=os.remove)


def _status_from_disk(pair_id: str, want: str):
    """Статус done из sidecar (после рестарта) или None.

    Возвращает done, только если sidecar-хеш совпал с нынешним состоянием
    и все файлы на месте и ненулевые.
    """
    ws = get_workspace(pair_id)
    if ws is None:
        return None
    return LEDGER.status_from_disk(ws, want)


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
    # Старые нарезки могли появиться до исправления прав: чиним при выдаче,
    # а не только в момент экспорта. Диагностика — в GET /api/v1/cache.
    export_access.ensure_access(
        ws.path,
        root=os.environ.get("VC_ACCESS_ANCHOR")
        or workspace.WORKSPACE_ROOT)
    export_access.ensure_file(full)
    return FileResponse(open(full, "rb"), as_attachment=True, filename=os.path.basename(full))