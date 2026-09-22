"""Провайдер кадров видео на сервере.

Отдаёт JPEG-кадр по индексу из файла на диске (original или visualization).

Движок — PyAV (FFmpeg). Индекс строится **одним проходом demux+decode**:
число демукс-пакетов не равно числу кадров, если в записи есть битые
access unit (пакет без картинки). Адресуем кадры по display-порядку успешно
декодированных PTS (``visible_pts``) — это и таймлайн, и то, что реально
видит пользователь; битые пакеты просто пропускаются, не сдвигая индексы.
Среди ключевых кадров (I-кадров) строится индекс границ GOP в видимом
пространстве, чтобы декодировать только кадры внутри одной группы.

Почему так: у H.264 не-ключевой кадр доступен только через декодирование
всей цепочки от ближайшего I-кадра. Один „холодный" запрос к середине GOP
требует декода всей группы (у crowd-подобных файлов GOP ~ 250 кадров,
1080p → 1.5–2 c — это и был источник «тормозов» при перемотке). Решение —
декодировать GOP один раз целиком, кэшировать её кадры в памяти (JPEG,
по ключу (GOP, quality, scale)) и префетчить соседние группы в фоне:

  * кэш JPEG-кадров группы (LRU, лимиты: ``CACHE_GOPS`` групп и
    ``CACHE_MB`` суммарного байтового объёма) — раскоды малы (~0.1-0.3 МБ/
    кадр), поэтому даже огромные группы (crowd 250 кадров ≈ 1.5 ГБ BGR, но
    ~40 МБ JPEG) вмещаются в отведённую память, и обратная перемотка идёт
    из памяти, а не передикодированием;
  * прогрессивная отдача: кадр уходит клиенту сразу после его энкода,
    остальные кадры группы дозаполняются тем же декодом;
  * фоновый префетч соседних GOP при обращении к g — по ходу
    подтверждённого обхода (два подряд шага одного знака): вперёд
    g+1 … g+PREFETCH_AHEAD, назад зеркально; без направления — вперёд;
  * декод групп параллелен по ядрам (пул от ``os.cpu_count()``,
    регулируется env ``VC_DECODE_CORES``); одновременных декодов строго
    ``DECODE_CORES`` (транзиентный BGR ~127 МБ/группа у 1600×1200);
  * точный seek к PTS ключевого кадра (без ``-1``: при захвате цели на
    самом I-кадре декодируется 1 кадр).
"""

from __future__ import annotations

import array
import bisect
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import av
import cv2

# --- Конфигурация (зависит от числа ядер/памяти, переопределяется env) ---
_DEFAULT_CORES = os.cpu_count() or 1
# Одновременных декодов GOP. Для 1080p-кадра транзиентно ~6 МБ BGR + накопленный
# JPEG группы, поэтому пул ограничиваем сверху независимо от числа ядер.
DECODE_CORES = int(os.environ.get("VC_DECODE_CORES") or min(8, _DEFAULT_CORES))
PREFETCH_AHEAD = int(os.environ.get("VC_PREFETCH_AHEAD") or 3)
# Пауза спроса (с): цепочный префетч продолжается, только пока кадры реально
# запрашивают (get_frame не старше N секунд). Иначе после паузы пул декодировал
# бы файл до конца — «все ядра и не останавливается».
PREFETCH_IDLE_S = float(os.environ.get("VC_PREFETCH_IDLE_S") or 5.0)
CACHE_MB = int(os.environ.get("VC_GOP_CACHE_MB") or 512)
CACHE_GOPS = int(os.environ.get("VC_GOP_CACHE_GOPS") or 16)
JPEG_QUALITY = int(os.environ.get("VC_JPEG_QUALITY") or 78)
# Масштаб кадров по умолчанию (1.0 = оригинал); фронтенд обычно передаёт свой.
FRAME_SCALE = float(os.environ.get("VC_FRAME_SCALE") or 1.0)

# Допустимые диапазоны per-request параметров (качество JPEG и масштаб).
QUALITY_MIN, QUALITY_MAX = 20, 95
SCALE_MIN, SCALE_MAX = 0.05, 1.0

_DECODE_POOL = ThreadPoolExecutor(max_workers=DECODE_CORES)
# Страховка по памяти: потоков декода не больше, чем DECODE_CORES, т.к. вся
# работа идёт через пул; транзиент на 1080p-группу ~6 МБ BGR + JPEG группы.
_HEAVY = threading.BoundedSemaphore(DECODE_CORES)


class _ByteBudget:
    """Отслеживание потребления BGR-памяти: транзит (в декоде) + кэш.

    Декодированные кадры крупные: 1600×1200 BGR → 5.8 МБ/кадр, GOP из 22
    кадров ≈ 127 МБ.  ``reserve()`` **не блокирует** — конкуренция за пул
    (``_HEAVY`` / ``DECODE_CORES``) уже ограничивает число одновременных
    декодов; здесь мы считаем байты, чтобы при попадании в кэш (land)
    вытеснять старьё при переполнении ``CACHE_GOPS`` / ``CACHE_MB``.
    """

    def __init__(self, limit_bytes: int) -> None:
        self._limit = max(int(limit_bytes), 1)
        self._transient = 0  # байты в декодируемых GOP (не блокирует)
        self._cached = 0     # байты в кэше
        self._cond = threading.Condition()

    def cached_bytes(self) -> int:
        with self._cond:
            return self._cached

    def pressured(self, frac: float = 0.8) -> bool:
        """Давление памяти: (кэш + транзит) выше доли лимита.

        Советующий флаг для admission-контроля префетча: спекулятивные
        декоды не должны вытеснять demand-GOP (LRU-thrash на больших GOP).
        """
        with self._cond:
            return (self._cached + self._transient) > self._limit * frac

    def reserve(self, nbytes: int) -> None:
        """Зафиксировать резерв под декод GOP.  Не блокирует — задача
        запускается немедленно; конкуренция идёт через ``_HEAVY``."""
        with self._cond:
            self._transient += min(nbytes, self._limit)

    def land_in_cache(self, reserved: int, actual: int) -> None:
        """Резерв «переезжает» из транзита в кэш."""
        with self._cond:
            self._transient = max(0, self._transient - reserved)
            self._cached += actual
            self._cond.notify_all()

    def release(self, reserved: int) -> None:
        """Отменить резерв (GOP не попала в кэш: ошибка/пусто)."""
        with self._cond:
            self._transient = max(0, self._transient - reserved)
            self._cond.notify_all()

    def evict_cached(self, nbytes: int) -> None:
        """Снять с кэша байты уже вытесненной GOP."""
        with self._cond:
            self._cached = max(0, self._cached - nbytes)
            self._cond.notify_all()


_BUDGET = _ByteBudget(CACHE_MB << 20)

# Кэш собранных индексов: построение делает полный проход декода (на 1600×1200
# ~15 c), поэтому повторное создание провайдера не должно декодировать файл
# заново. Ключ — (абсолютный путь, размер, mtime_ns): при замене файла ключ
# меняется и индекс пересобирается.
_INDEX_CACHE: "OrderedDict[tuple, _Index]" = OrderedDict()
_INDEX_CACHE_LOCK = threading.Lock()
_INDEX_CACHE_MAX = int(os.environ.get("VC_INDEX_CACHE") or 16)


def _frame_cache_dir() -> str:
    """Каталог дискового кэша индексов (переживает рестарт процесса)."""
    d = os.environ.get("VC_FRAMECACHE_DIR")
    if d:
        return d
    root = os.environ.get("VC_WORKSPACE_ROOT")
    if root:
        return os.path.join(root, ".framecache")
    return os.path.join(tempfile.gettempdir(), "vc_framecache")


_FRAME_CACHE_DIR = _frame_cache_dir()


def _cache_key(path: str) -> str:
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest() + ".json"


def _cache_load(path: str, size, mtime_ns):
    """Читает кэш видимых PTS, если он соответствует текущей версии файла."""
    if size is None:
        return None
    try:
        with open(os.path.join(_FRAME_CACHE_DIR, _cache_key(path)),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        if (data.get("v") != 1
                or data.get("path") != os.path.abspath(path)
                or data.get("size") != size
                or data.get("mtime_ns") != mtime_ns):
            return None
        return (int(data["packet_total"]),
                array.array("q", data["visible_pts"]),
                array.array("q", data["kf_pts"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _cache_save(path: str, size, mtime_ns, packet_total, visible, kf_us) -> None:
    """Best-effort запись кэша (атомарно через .tmp)."""
    if size is None:
        return
    try:
        os.makedirs(_FRAME_CACHE_DIR, exist_ok=True)
        cp = os.path.join(_FRAME_CACHE_DIR, _cache_key(path))
        with open(cp + ".tmp", "w", encoding="utf-8") as f:
            json.dump({
                "v": 1,
                "path": os.path.abspath(path),
                "size": size,
                "mtime_ns": mtime_ns,
                "packet_total": packet_total,
                "visible_pts": list(visible),
                "kf_pts": list(kf_us),
            }, f, separators=(",", ":"))
        os.replace(cp + ".tmp", cp)
    except OSError:
        pass


def _make_bounds(visible, kf_us) -> List[int]:
    """Границы GOP (в видимых индексах) по PTS ключевых кадров.

    Ключевой пакет мог быть битым — тогда граница указывает на первый
    уцелевший кадр группы.
    """
    total = len(visible)
    if not total:
        return [0]
    kf_idx = sorted({bisect.bisect_left(visible, t) for t in kf_us})
    kf_idx = [b for b in kf_idx if 0 <= b < total]
    if not kf_idx or kf_idx[0] != 0:
        kf_idx = [0] + kf_idx
    if kf_idx[-1] != total:
        kf_idx.append(total)
    return kf_idx


def _build_index(path: str) -> _Index:
    """Собрать индекс, переиспользуя in-memory кэш по (path, size, mtime_ns)."""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return _Index(path)

    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is not None:
            _INDEX_CACHE.move_to_end(key)
            return idx

    idx = _Index(path)
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE[key] = idx
        _INDEX_CACHE.move_to_end(key)
        while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)
    return idx


class _Index:
    """Индекс кадров: видимые (декодируемые) PTS и границы GOP.

    Три разные величины:

    * ``packet_total`` — число демукс-пакетов (по заголовку/пакетами);
    * ``total`` — число реально декодируемых кадров (таймлайн, адресация);
    * ``skipped`` — пакеты без картинки (битые access unit).

    ``visible_pts`` хранит PTS каждого видимого кадра в display-порядке
    (целые микросекунды) — именно по нему строится ``idx -> PTS`` и границы
    GOP. Битый пакет не даёт кадра, но и не сдвигает индексы остальных:
    соседние кадры сохраняют свои номера.
    """

    def __init__(self, path: str) -> None:
        try:
            cont = av.open(path)
        except Exception as e:
            raise ValueError(f"Ошибка открытия видеофайла: {path} ({e})")
        stream = cont.streams.video[0]
        self.tb = stream.time_base
        self.width = stream.codec_context.width
        self.height = stream.codec_context.height
        rate = stream.average_rate
        self.fps = float(rate) if rate else 0.0

        try:
            st = os.stat(path)
            size, mtime_ns = st.st_size, st.st_mtime_ns
        except OSError:
            size = mtime_ns = None

        cached = _cache_load(path, size, mtime_ns)
        if cached is not None:
            packet_total, visible, kf_us = cached
            cont.close()
        else:
            tb = float(self.tb)
            packet_total = 0
            kf_us = array.array("q")
            visible = array.array("q")

            # Один проход: демукс-пакеты и их декод. Декодируем per-packet,
            # чтобы битый access unit (InvalidDataError) не обрывал весь файл,
            # а лишь пропускался — так же, как делает ffmpeg при подсчёте.
            for pkt in cont.demux(stream):
                if pkt.pts is None:
                    continue
                packet_total += 1
                t = pkt.pts * float(pkt.time_base or self.tb)
                if pkt.is_keyframe:
                    kf_us.append(round(t * 1e6))
                try:
                    for fr in stream.decode(pkt):
                        ts = (fr.pts or 0) * float(fr.time_base or tb)
                        visible.append(round(ts * 1e6))
                except av.InvalidDataError:
                    continue
            # Хвост декодера (отложенные B-кадры): иначе теряются последние.
            try:
                for fr in stream.decode(None):
                    ts = (fr.pts or 0) * float(fr.time_base or tb)
                    visible.append(round(ts * 1e6))
            except av.FFmpegError:
                pass
            cont.close()
            _cache_save(path, size, mtime_ns, packet_total, visible, kf_us)

        self.packet_total = packet_total
        self.visible_pts = visible  # array('q'), микросекунды, display-порядок
        self.total = len(visible)
        self.skipped = max(0, self.packet_total - self.total)
        self.bounds: List[int] = _make_bounds(visible, kf_us)

    def gop_of(self, frame: int) -> Optional[int]:
        """Номер GOP, содержащего кадр, или None (до первого ключевого)."""
        if not 0 <= frame < self.total:
            return None
        g = bisect.bisect_right(self.bounds, frame) - 1
        return g if 0 <= g < len(self.bounds) - 1 else None


class _GopTask:
    """Текущий декод одной GOP: кадры появляются по мере декодирования.

    Формат хранения — JPEG (под quality/scale запроса): компактно, в кэш
    вмещаются даже огромные GOP (crowd 250 кадров ≈ 1.5 ГБ BGR, но ~40 МБ
    JPEG), поэтому перемотка назад не передикодирует. Кодирование выполняет
    поток декода (по параметрам первого обращения к группе).
    """

    def __init__(self, quality: int, scale: float) -> None:
        self.frames: Dict[int, bytes] = {}
        self._events: Dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self.error: Optional[BaseException] = None
        self.done = False
        self.abandoned = False  # декод брошен: спрос умер, кэшируем частичное
        self.waiters = 0  # заблокированных ждунов (защита живого спроса)
        self.reserved = 0  # байты, зарезервированные в _ByteBudget под этот кэш
        self.quality = quality
        self.scale = scale

    def put(self, frame: int, bgr: "numpy.ndarray") -> None:
        jpeg = _Provider._encode(bgr, self.quality, self.scale)
        if jpeg is None:
            return
        with self._lock:
            self.frames[frame] = jpeg
            ev = self._events.pop(frame, None)
        if ev is not None:
            ev.set()

    def mark_done(self) -> None:
        with self._lock:
            self.done = True

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            self.error = exc
            self.done = True

    def wait(self, frame: int) -> Optional[bytes]:
        """Ждать JPEG-кадр (или завершение декода без него)."""
        with self._lock:
            self.waiters += 1
        try:
            while True:
                with self._lock:
                    j = self.frames.get(frame)
                    err = self.error
                    done = self.done
                if j is not None:
                    return j
                if err is not None or done:
                    return None
                self._event_for(frame).wait(0.15)
        finally:
            with self._lock:
                self.waiters -= 1

    def orphaned(self) -> bool:
        """Задачу никто не ждёт (кандидат на аборт при мёртвом спросе)."""
        with self._lock:
            return self.waiters == 0

    def _event_for(self, frame: int) -> threading.Event:
        with self._lock:
            ev = self._events.get(frame)
            if ev is None:
                ev = threading.Event()
                self._events[frame] = ev
            return ev


class _Provider:
    """Провайдер одного видеофайла: индекс + кэш GOP (JPEG) + префетч.

    Группы кэшируются как **готовые JPEG** под ключом (GOP, quality, scale):
    декодируем BGR один раз, а кадры храним сжатыми. Это позволяет вмещать
    огромные группы (crowd 250 кадров ≈ 1.5 ГБ BGR, но ~40 МБ JPEG) и делает
    обратную перемотку дешёвой — без повторного декода. Смена ползунков
    (quality/scale) создаёт отдельный кэш, но пока настройки фиксированы,
    перемотка в обе стороны идёт из памяти.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._index: Optional[_Index] = None
        self._ilock = threading.Lock()
        self._cache: "OrderedDict[tuple, Dict[int, bytes]]" = OrderedDict()
        self._gop_sizes: Dict[tuple, int] = {}  # фактические байты GOP в кэше
        self._tasks: Dict[tuple, _GopTask] = {}
        self._scheduled: set[tuple] = set()
        # Детектор шага для направленного префетча: воспроизведение идёт
        # строго ±1 (см. nextPlayPosition во фронте), прыжки — большие
        # дельты. Два подряд шага одного знака = подтверждённый обход.
        self._last_idx: Optional[int] = None
        self._last_step: int = 0
        self._walk_dir: int = 0  # последнее подтверждённое направление
        self._last_demand = 0.0  # monotonic-время последнего get_frame

    @staticmethod
    def _key(g: int, quality: int, scale: float) -> tuple:
        """Ключ кэша GOP: номер группы + параметры просмотра (масштаб округлён)."""
        return (g, int(quality), round(float(scale), 2))

    @staticmethod
    def _gop_bytes(idx, g: int) -> int:
        n = min(idx.bounds[g + 1], idx.total) - idx.bounds[g]
        return n * idx.width * idx.height * 3

    # --- индекс ---
    def _ensure_index(self) -> _Index:
        if self._index is None:
            with self._ilock:
                if self._index is None:
                    self._index = _build_index(self.path)
        return self._index

    # --- кэш ---
    def _cache_hit(self, key: tuple, index: int) -> Optional[bytes]:
        with self._ilock:
            d = self._cache.get(key)
            if d is None:
                return None
            self._cache.move_to_end(key)
            return d.get(index)

    def _cache_put(self, key: tuple, frames: Dict[int, bytes],
                   reserved: int) -> None:
        actual = sum(len(v) for v in frames.values())
        with self._ilock:
            if key in self._cache:
                _BUDGET.release(reserved)
                return
            self._cache[key] = frames
            self._gop_sizes[key] = actual
        _BUDGET.land_in_cache(reserved, actual)
        self._evict_to_limit()

    def _evict_to_limit(self) -> None:
        """Вытеснять старые GOP, пока кэш ≤ CACHE_GOPS и ≤ CACHE_MB."""
        while True:
            cached = _BUDGET.cached_bytes()
            with self._ilock:
                if len(self._cache) <= CACHE_GOPS and \
                        cached <= (CACHE_MB << 20):
                    return
                if not self._cache:
                    return
                key, _ = self._cache.popitem(last=False)
                size = self._gop_sizes.pop(key, 0)
            if size:
                _BUDGET.evict_cached(size)

    # --- декод GOP ---
    @staticmethod
    def _encode(bgr, quality: int, scale: float) -> Optional[bytes]:
        """JPEG из готового BGR-массива под запрошенные quality/scale."""
        if scale < (1.0 - 1e-6):
            h, w = bgr.shape[:2]
            nh, nw = max(1, round(h * scale)), max(1, round(w * scale))
            if (nh, nw) != (h, w):
                bgr = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return buf.tobytes() if ok else None

    def _decode_gop(self, g: int, task: _GopTask) -> None:
        idx = self._ensure_index()
        start = idx.bounds[g]
        end = min(idx.bounds[g + 1], idx.total)
        tb = float(idx.tb)
        vpts = idx.visible_pts
        seek_tick = int((vpts[start] / 1e6) / tb + 0.5)
        try:
            # Резервируем место под BGR-массивы на время декода; при попадании
            # в кэш JPEG-байты занимают меньше, разница освобождается.
            task.reserved = self._gop_bytes(idx, g)
            _BUDGET.reserve(task.reserved)
            with _HEAVY:
                cont = av.open(self.path)
                try:
                    stream = cont.streams.video[0]
                    cont.seek(seek_tick, stream=stream, backward=True, any_frame=False)
                    done = False
                    # Per-packet декод: битый access unit группы лишь
                    # пропускается, остальные кадры группы не теряются.
                    # Аборт: спрос умер (пауза) И задачу никто не ждёт —
                    # бросаем декод, кэшируем частичное; ждуны недодекодированных
                    # кадров получат None. Живой спрос (waiters > 0) не трогаем:
                    # окну attach'а всегда хватает свежего demand-метки входа.
                    for pkt in cont.demux(stream):
                        if task.orphaned() and self._provider_idle():
                            task.abandoned = True
                            done = True
                            break
                        if done:
                            break
                        try:
                            for fr in stream.decode(pkt):
                                fs = (fr.pts or 0) * float(fr.time_base or tb)
                                vi = bisect.bisect_left(vpts, round(fs * 1e6))
                                if vi < start:
                                    continue  # B-кадры предыдущей GOP (display)
                                if vi >= end:
                                    done = True
                                    break
                                task.put(vi, fr.to_ndarray(format="bgr24"))
                        except av.InvalidDataError:
                            continue
                    # Хвост декодера: последние B-кадры группы.
                    if not done and self._demand_alive():
                        try:
                            for fr in stream.decode(None):
                                fs = (fr.pts or 0) * float(fr.time_base or tb)
                                vi = bisect.bisect_left(vpts, round(fs * 1e6))
                                if vi < start:
                                    continue
                                if vi >= end:
                                    break
                                task.put(vi, fr.to_ndarray(format="bgr24"))
                        except av.FFmpegError:
                            pass
                finally:
                    cont.close()
            task.mark_done()
        except BaseException as e:  # noqa: BLE001 — завершение задачи для ждунов
            task.fail(e)

    def _run_pooled(self, key: tuple, task: _GopTask) -> None:
        g, _, _ = key
        self._decode_gop(g, task)
        self._finish_task(key, task)

    def _walk_direction(self, index: int) -> int:
        """Направление подтверждённого обхода: +1 / -1 / 0 (неизвестно).

        Подтверждение — два подряд ненулевых шага одного знака (такие даёт
        воспроизведение: кадры запрашиваются строго по порядку). Первый
        запрос, повтор кадра и смена знака/с прыжком дают 0 — поведение
        префетча как раньше (вперёд). Только производительность, на
        корректность отдачи не влияет.
        """
        with self._ilock:
            step = 0 if self._last_idx is None else index - self._last_idx
            if step != 0 and self._last_step != 0 \
                    and (step > 0) == (self._last_step > 0):
                direction = 1 if step > 0 else -1
                self._walk_dir = direction
            else:
                direction = 0
                if step != 0:
                    # Прыжок/смена знака/первый шаг — подтверждённого
                    # направления больше нет.
                    self._walk_dir = 0
            if step != 0:
                self._last_step = step
            self._last_idx = index
            return direction

    def _walk_direction_peek(self) -> int:
        """Последнее подтверждённое направление без обновления состояния
        (для цепочного префетча из пула)."""
        with self._ilock:
            return self._walk_dir

    def _finish_task(self, key: tuple, task: _GopTask) -> None:
        with self._ilock:
            self._tasks.pop(key, None)
            self._scheduled.discard(key)
        if task.error is not None:
            logger.warning("gop task failed %s: %r", key, task.error)
        if not task.error and task.frames:
            self._cache_put(key, task.frames, task.reserved)
        elif task.reserved:
            # Освобождаем и неизрасходованную часть резерва (JPEG < BGR).
            _BUDGET.release(task.reserved)
        g, q, s = key
        # Цепочный префетч — пока провайдер не простаивает (есть ждуны или
        # свежий спрос). После паузы ждуны рассасываются, гейт встаёт.
        if not self._provider_idle():
            self._prefetch(g, q, s, self._walk_direction_peek())

    def _prefetch(self, g: int, quality: int, scale: float,
                   direction: int = 1) -> None:
        """Прогреть соседние GOP в фоне: вперёд (direction >= 0, по
        умолчанию — как раньше) или назад (direction < 0).

        Admission-контроль: при давлении памяти спекулятивные декоды не
        стартуют (иначе вытесняют demand-GOP и каждый кадр декодируется
        заново — коллапс вместо ускорения).
        """
        if _BUDGET.pressured():
            return
        idx = self._ensure_index()
        step = -1 if direction < 0 else 1
        for d in range(1, PREFETCH_AHEAD + 1):
            ng = g + step * d
            if ng < 0 or ng >= len(idx.bounds) - 1:
                break
            nkey = self._key(ng, quality, scale)
            with self._ilock:
                if nkey in self._cache or nkey in self._tasks or nkey in self._scheduled:
                    continue
                self._scheduled.add(nkey)
                task = _GopTask(quality, scale)
                self._tasks[nkey] = task
            try:
                _DECODE_POOL.submit(self._run_pooled, nkey, task)
            except Exception:
                # Пул остановлен (завершение приложения) — убираем как несостоявшийся.
                with self._ilock:
                    self._tasks.pop(nkey, None)
                    self._scheduled.discard(nkey)

    # --- чтение кадра ---
    def _note_demand(self) -> None:
        self._last_demand = time.monotonic()

    def _demand_alive(self) -> bool:
        return (time.monotonic() - self._last_demand) <= PREFETCH_IDLE_S

    def _provider_idle(self) -> bool:
        """Никто ничего не ждёт (ни на одной задаче) и свежих обращений нет.

        Именно это — сигнал «можно бросать/не продолжать»: счётчик времени
        входов сам по себе врёт при секвенциальном клиенте (следующий вход
        только после ответа, а ответ медленный без префетча — deadlock
        голодания). Ждуны — честный признак живого спроса.
        """
        with self._ilock:
            if any(t.waiters > 0 for t in self._tasks.values()):
                return False
        return not self._demand_alive()

    def _index_if_ready(self) -> Optional[_Index]:
        """Индекс, если уже собран (без блокировки на сборке)."""
        with self._ilock:
            return self._index

    def get_frame(self, index: int, quality: int = JPEG_QUALITY,
                  scale: float = FRAME_SCALE) -> tuple:
        """Возвращает (jpeg|None, info): info = {source, abandoned}.

        ``source``: cache | task | single | range | abandoned.
        """
        self._note_demand()
        idx = self._ensure_index()
        if not 0 <= index < idx.total:
            return None, {"source": "range", "abandoned": False}

        g = idx.gop_of(index)
        direction = self._walk_direction(index)
        if g is not None:
            key = self._key(g, quality, scale)
            j = self._cache_hit(key, index)
            if j is not None:
                # Группа в кэше — прогреваем соседей по ходу обхода заранее,
                # чтобы перемотка дальше не упиралась в холодную границу.
                self._prefetch(g, quality, scale, direction)
                return j, {"source": "cache", "abandoned": False}
            task = self._task_for(g, quality, scale, direction)
            if task is not None:
                j = task.wait(index)
                if j is not None:
                    return j, {"source": "task", "abandoned": False}
                if task.abandoned:
                    return None, {"source": "abandoned", "abandoned": True}
        single = self._read_one(index, quality, scale)
        return single, {"source": "single", "abandoned": False}

    def _task_for(self, g: int, quality: int, scale: float,
                  direction: int = 1) -> Optional[_GopTask]:
        """Создать (и запустить в пуле) задачу GOP, если её ещё нет."""
        key = self._key(g, quality, scale)
        with self._ilock:
            task = self._tasks.get(key)
            if task is None:
                task = _GopTask(quality, scale)
                self._tasks[key] = task
                created = True
            else:
                created = False
        if created:
            try:
                _DECODE_POOL.submit(self._run_pooled, key, task)
            except Exception:
                # Пул остановлен (завершение приложения) — как несостоявшийся.
                with self._ilock:
                    self._tasks.pop(key, None)
                    self._scheduled.discard(key)
                return None
            # Соседние группы прогреваем сразу — параллельно текущему декоду,
            # тогда переход через границу не застанет холодную группу.
            self._prefetch(g, quality, scale, direction)
        return task

    # --- одиночный seek (до первого ключевого кадра и прочие огрехи) ---
    def _read_one(self, index: int, quality: int, scale: float) -> Optional[bytes]:
        idx = self._ensure_index()
        if not 0 <= index < idx.total:
            return None
        tb = float(idx.tb)
        vpts = idx.visible_pts
        target_us = vpts[index]
        g = idx.gop_of(index)
        seek_us = vpts[idx.bounds[g]] if g is not None else vpts[0]
        seek_tick = int((seek_us / 1e6) / tb + 0.5)
        try:
            cont = av.open(self.path)
            try:
                stream = cont.streams.video[0]
                cont.seek(seek_tick, stream=stream, backward=True, any_frame=False)
                for pkt in cont.demux(stream):
                    try:
                        for fr in stream.decode(pkt):
                            fs = (fr.pts or 0) * float(fr.time_base or tb)
                            if round(fs * 1e6) >= target_us:
                                return self._encode(
                                    fr.to_ndarray(format="bgr24"), quality, scale)
                    except av.InvalidDataError:
                        continue
            finally:
                cont.close()
        except (av.FFmpegError, ValueError):
            return None
        return None

    def metadata(self) -> dict:
        return _index_meta(self._ensure_index())


# --- регистр провайдеров ---
_providers: "Dict[str, _Provider]" = {}
_providers_lock = threading.Lock()


def _get_provider(path: str) -> _Provider:
    with _providers_lock:
        p = _providers.get(path)
        if p is None:
            p = _Provider(path)
            _providers[path] = p
        return p


def get_frame_jpeg(path: str, index: int, *, quality: int = JPEG_QUALITY,
                   scale: float = FRAME_SCALE, want_info: bool = False):
    """Возвращает (jpeg_bytes, mime) для кадра по индексу или (None, None).

    ``quality`` — качество JPEG; ``scale`` — масштаб кадра (0.05..1.0):
    даунскейл применяется до энкода, кадр уходит клиенту уже уменьшенным.
    ``want_info=True`` — третьим элементом вернуть info {source, abandoned}.
    """
    jpeg, info = _get_provider(path).get_frame(index, quality, scale)
    if want_info:
        return (jpeg, "image/jpeg", info) if jpeg else (None, None, info)
    return (jpeg, "image/jpeg") if jpeg else (None, None)


def warm_gop(path: str, index: int = 0, quality: int = JPEG_QUALITY,
             scale: float = FRAME_SCALE) -> None:
    """Прогреть GOP кадра в фоне (к открытию редактора): без ожидания.

    Без готового индекса — пропуск (индекс строится фоном отдельно).
    Никогда не бросает исключения (fire-and-forget).
    """
    try:
        prov = _get_provider(path)
        idx = prov._index_if_ready()
        if idx is None or not 0 <= index < idx.total:
            return
        g = idx.gop_of(index)
        if g is None:
            return
        prov._task_for(g, quality, scale, prov._walk_direction_peek())
    except Exception:  # noqa: BLE001 — прогрев best-effort
        pass


def get_metadata(path: str) -> dict:
    return _get_provider(path).metadata()


logger = logging.getLogger(__name__)

# Фоновая сборка индексов: путь -> поток уже запущен. При ошибке сборки
# путь убирается из множества (повтор разрешён), а текст ошибки запоминается
# (список показывает такую задачу как битую, а не вечно индексирующуюся).
# Битый файл падает быстро (на av.open), поэтому шторма потоков нет.
_BG_INDEX_LOCK = threading.Lock()
_BG_INDEX_STARTED: "set[str]" = set()
_BG_INDEX_FAILED: "Dict[str, str]" = {}


def _index_meta(idx: _Index) -> dict:
    return {
        "width": idx.width,
        "height": idx.height,
        "total_frames": idx.total,
        "packet_frames": idx.packet_total,
        "skipped_frames": idx.skipped,
        "fps": idx.fps,
    }


def ensure_index_background(path: str) -> None:
    """Построить индекс файла в фоне (один раз на путь; демон-поток)."""
    key = os.path.abspath(path)
    with _BG_INDEX_LOCK:
        if key in _BG_INDEX_STARTED:
            return
        _BG_INDEX_STARTED.add(key)
    threading.Thread(target=_bg_index_build, args=(key,), daemon=True).start()


def _bg_index_build(key: str) -> None:
    try:
        _get_provider(key)._ensure_index()
        with _BG_INDEX_LOCK:
            _BG_INDEX_FAILED.pop(key, None)
    except Exception as e:  # noqa: BLE001 — битый файл: запомнить ошибку
        logger.warning("background index failed: %s (%r)", key, e)
        with _BG_INDEX_LOCK:
            _BG_INDEX_STARTED.discard(key)
            _BG_INDEX_FAILED[key] = f"{type(e).__name__}: {e}"[:300]


def index_error(path: str) -> Optional[str]:
    """Текст ошибки фоновой сборки индекса (None — нет ошибки)."""
    return _BG_INDEX_FAILED.get(os.path.abspath(path))


def try_get_metadata(path: str) -> Optional[dict]:
    """Метаданные без долгого ожидания: быстрый путь (in-memory или
    дисковый кэш) либо None + запуск фоновой сборки.

    Полный проход demux+decode (минуты на сотнях мегабайт) никогда не
    выполняется в этом вызове — для него есть фон.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is not None:
            _INDEX_CACHE.move_to_end(key)
            return _index_meta(idx)
    # Дисковый кэш валиден? Тогда сборка — это быстрый парс JSON.
    if _cache_load(path, st.st_size, st.st_mtime_ns) is not None:
        try:
            return _index_meta(_build_index(path))
        except Exception:  # noqa: BLE001 — ниже уйдёт в фон
            pass
    ensure_index_background(path)
    return None


def close_source(path: str) -> None:
    with _providers_lock:
        _providers.pop(path, None)