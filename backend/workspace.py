# -*- coding: utf-8 -*-
"""Stateless workspace: сканер директорий + TSV-хранилище фрагментов.

Workspace — это папка на диске с видеофайлами и fragments.tsv.
Никакой БД, никакого ORM. Метаданные вычисляются на лету через PyAV.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import project_meta
import task_meta
from vc_pairs import frame_provider
from videocutter.standalone import workspace as ws_fs
from videocutter.standalone.workspace import VIDEO_EXTS

logger = logging.getLogger(__name__)

# Корневая директория со всеми workspace-ами.
WORKSPACE_ROOT = os.getenv("VC_WORKSPACE_ROOT", os.path.join(os.path.dirname(__file__), "workspaces"))

# Имя файла с фрагментами (аналог txt-файла в PVC).
FRAGMENTS_FILE = "fragments.tsv"

# Настройки просмотра по умолчанию (для задач без строки `# settings` в tsv).
DEFAULT_QUALITY = 78
DEFAULT_SCALE = 0.75

# Границы настроек, принимаемых от клиента.
QUALITY_MIN, QUALITY_MAX = 20, 95
SCALE_MIN, SCALE_MAX = 0.05, 1.0


def roles_of_paths(ws: Workspace, paths) -> str:
    """Какие роли покрыты сломанными путями: 'source' | 'preview' | 'both' | ''.

    Один файл может играть обе роли (нет отдельного превью) — тогда он и
    сломан, а чинить надо источник: замена источника переопределит и
    превью, замена превью оставит сломанный файл на месте.
    """
    src = ws.original or ""
    prev = ws.visualization or ""
    roles = set()
    for path in paths:
        if not path:
            continue
        if path == src:
            roles.add("source")
        elif path == prev:
            roles.add("preview")
    if not roles:
        return ""
    if len(roles) > 1:
        return "both"
    return roles.pop()


def broken_role(ws: Workspace) -> str:
    """Какая роль задачи сломана: 'source' | 'preview' | 'both' | ''.

    Основание — файлы на диске: индекс не собрался ровно по нечитаемому
    файлу (сборка падает на ``av.open``), и тот же путь помечен в реестре
    фоновых сборок; плюс путь файла из записанной ошибки проверки. Роль
    выводится сравнением с ролями workspace на диске — каждый раз заново,
    своего хранилища ролей не заводим (у трубы памяти нет: состояние живёт
    в файлах и в кэше индекса).
    """
    paths = [p for p in (ws.original, ws.visualization)
             if p and frame_provider.index_error(p)]
    err_path = workspace_error_path(ws.name)
    if err_path:
        paths.append(err_path)
    return roles_of_paths(ws, paths)


@dataclass
class Workspace:
    """Один workspace: папка с видео + fragments.tsv."""

    name: str  # Имя директории (id workspace).
    path: str  # Абсолютный путь к директории.

    # Заполняются лениво при first access.
    _original: Optional[str] = field(default=None, repr=False)
    _visualization: Optional[str] = field(default=None, repr=False)
    _unassigned: Optional[List[str]] = field(default=None, repr=False)
    _meta: Optional[dict] = field(default=None, repr=False)
    # Снятые идентичности для инвалидации ручных правок: состав каталога
    # (роли) и файл превью (метаданные). Без сверки долгоживущий объект
    # из реестра отдавал бы stale после замены/добавления файлов мимо API.
    _videos_mtime: Optional[float] = field(default=None, repr=False)
    _meta_key: Optional[tuple] = field(default=None, repr=False)
    _fragments: Optional[List[dict]] = field(default=None, repr=False)
    _position: Optional[int] = field(default=None, repr=False)
    _settings: Optional[tuple] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _discover_videos(self) -> None:
        """Находит роли видео в директории: source (original), preview, unassigned.

        Роли кодируются именами файлов: унифицированные ``source.<ext>`` /
        ``preview.<ext>``. Одиночный файл без маркера — «нейтральный»
        (играет обе роли). Состав каталога сверяется по его mtime: ручное
        добавление/удаление файлов мимо API переоткрывает роли (дешёвый
        stat + readdir маленькой папки).
        """
        try:
            dir_mtime = os.path.getmtime(self.path)
        except OSError:
            dir_mtime = None
        if self._original is not None and (
                dir_mtime is None or dir_mtime == self._videos_mtime):
            # Состав не менялся — или stat недоступен (держим мемоизированное,
            # как раньше: сбой stat не повод ронять роли).
            return

        found = ws_fs.classify_videos(WORKSPACE_ROOT, self.name)
        self._original = found["source"] or ""
        self._visualization = found["preview"] or ""
        self._unassigned = found["unassigned"]
        self._videos_mtime = dir_mtime
        # Роли могли смениться — метаданные превью тоже пересчитать.
        self._meta = None
        self._meta_key = None

    def invalidate_videos(self) -> None:
        """Сбрасывает кэш ролей и метаданных (после замены/переименования)."""
        self._original = None
        self._visualization = None
        self._unassigned = None
        self._meta = None
        self._videos_mtime = None
        self._meta_key = None

    @property
    def original(self) -> str:
        self._discover_videos()
        return self._original

    @property
    def visualization(self) -> str:
        self._discover_videos()
        return self._visualization

    @property
    def unassigned(self) -> List[str]:
        """Нейтральные видеофайлы без роли (для назначения роли в UI)."""
        self._discover_videos()
        return self._unassigned or []

    @property
    def has_visualization(self) -> bool:
        return bool(self.visualization)

    def metadata(self) -> dict:
        """Метаданные видео-превью (того, что смотрит пользователь).

        Номера кадров ведём по превью (как в PyVideoCutter: ``total_frames``
        берётся из превью), а нарезаем — из source. Запомненные метаданные
        сверяются с идентичностью файла (размер, mtime): ручная замена
        видео мимо API сбрасывает кэш, иначе detail врал бы старыми
        width/height/total_frames до рестарта.
        """
        preview = self.visualization or self.original
        key = None
        if preview:
            try:
                st = os.stat(preview)
                key = (st.st_size, st.st_mtime_ns)
            except OSError:
                key = None
        if self._meta is not None and (not preview or key == self._meta_key):
            return self._meta

        self._meta = self.video_metadata("preview")
        self._meta_key = key if preview else None
        return self._meta

    def video_metadata(self, role: str) -> dict:
        """Метаданные конкретной роли: ``source`` — исходник, ``preview`` —
        превью (если его нет — исходник). Видео без кадров даёт нули."""
        empty = {
            "total_frames": 0, "packet_frames": 0, "skipped_frames": 0,
            "width": 0, "height": 0, "fps": 0.0,
        }
        if role == "source":
            path = self.original
        else:
            path = self.visualization or self.original
        if not path or not os.path.isfile(path):
            return empty
        return frame_provider.get_metadata(path)

    def fragments_path(self) -> str:
        return os.path.join(self.path, FRAGMENTS_FILE)

    def _read(self) -> tuple[List[dict], int]:
        """Читает fragments.tsv: метаданные `# position` + таблицу фрагментов.

        Строки, начинающиеся с `#`, — комментарии/метаданные, в таблицу не
        попадают (поэтому формат остаётся обычным TSV с заголовком).
        Отсутствие строки `# position` → позиция 0 (обратная совместимость).
        """
        if self._fragments is not None:
            return self._fragments, self._position or 0

        tsv = self.fragments_path()
        frags: List[dict] = []
        position = 0
        quality, scale = DEFAULT_QUALITY, DEFAULT_SCALE

        if os.path.isfile(tsv):
            with open(tsv, newline="", encoding="utf-8") as f:
                data_lines: List[str] = []
                for line in f:
                    if line.startswith("#"):
                        key, _, value = line[1:].partition("\t")
                        key = key.strip()
                        if key == "position":
                            try:
                                position = int(value.strip())
                            except ValueError:
                                pass
                        elif key == "settings":
                            parts = value.split("\t")
                            if len(parts) >= 2:
                                try:
                                    q, s = int(parts[0]), float(parts[1])
                                    quality = min(QUALITY_MAX, max(QUALITY_MIN, q))
                                    scale = min(SCALE_MAX, max(SCALE_MIN, s))
                                except ValueError:
                                    pass
                        continue
                    data_lines.append(line)

                for row in csv.DictReader(data_lines, delimiter="\t"):
                    try:
                        frags.append({
                            "start": int(row["start"]),
                            "end": int(row["end"]),
                            "comment": row.get("comment", ""),
                        })
                    except (KeyError, ValueError):
                        continue  # Пропускаем битые строки.

        frags.sort(key=lambda x: (x["start"], x["end"]))
        self._fragments = frags
        self._position = position
        self._settings = (quality, scale)
        return frags, position

    def load_fragments(self) -> List[dict]:
        """Читает fragments.tsv → список {"start": int, "end": int, "comment": str}."""
        with self._lock:
            return self._read()[0]

    def load_position(self) -> int:
        """Текущий кадр, на котором пользователь завершил работу (по умолчанию 0)."""
        with self._lock:
            return self._read()[1]

    def load_settings(self) -> tuple[int, float]:
        """Настройки просмотра (quality, scale), сохранённые в tsv."""
        with self._lock:
            self._read()
            return self._settings or (DEFAULT_QUALITY, DEFAULT_SCALE)

    def save_settings(self, quality: int, scale: float) -> None:
        """Сохраняет настройки просмотра в tsv (не трогая фрагменты/позицию)."""
        q = min(QUALITY_MAX, max(QUALITY_MIN, int(quality)))
        s = min(SCALE_MAX, max(SCALE_MIN, float(scale)))
        with self._lock:
            frags = self._fragments if self._fragments is not None else self._read()[0]
            pos = self._position if self._position is not None else self._read()[1]
            self._settings = (q, s)
            self._write(frags, pos, q, s)

    def _write(self, fragments: List[dict], position: int,
               quality: Optional[int] = None, scale: Optional[float] = None) -> None:
        """Пишет fragments.tsv: строки `# position` + `# settings` + таблицу.

        Если настройки не переданы — сохраняются текущие (из кэша или файла),
        чтобы при сохранении фрагментов строка `# settings` не терялась.
        """
        if quality is None or scale is None:
            self._read()  # заполняет self._settings из файла/кэша
            quality, scale = self._settings or (DEFAULT_QUALITY, DEFAULT_SCALE)
        tsv = self.fragments_path()
        os.makedirs(os.path.dirname(tsv) or ".", exist_ok=True)
        with open(tsv, "w", newline="", encoding="utf-8") as f:
            f.write(f"# position\t{int(position)}\n")
            f.write(f"# settings\t{int(quality)}\t{float(scale):g}\n")
            writer = csv.DictWriter(f, fieldnames=["start", "end", "comment"], delimiter="\t")
            writer.writeheader()
            for frag in fragments:
                writer.writerow({
                    "start": frag["start"],
                    "end": frag["end"],
                    "comment": frag.get("comment", ""),
                })
        self._fragments = None  # Инвалидируем кэш.
        self._position = None
        self._settings = None

    def save_fragments(self, fragments: List[dict], position: Optional[int] = None) -> None:
        """Сохраняет фрагменты; позицию берёт из аргумента или из кэша."""
        with self._lock:
            current = self._read()[1]
            self._write(fragments, current if position is None else position)

    def save_position(self, position: int) -> None:
        """Сохраняет текущий кадр, не трогая список фрагментов."""
        with self._lock:
            frags = self._read()[0]
            self._write(frags, position)

    def invalidate_cache(self) -> None:
        """Сбрасывает кэш фрагментов, позиции и настроек (при внешних изменениях)."""
        self._fragments = None
        self._position = None
        self._settings = None


#: Control-символы, недопустимые в разметке (таб/перевод строки/CR — свои).
#: NUL в частности: C-читатели обрывают строку на \x00 — содержимое после
#: него «исчезает» незаметно; csv-акселератор такое не роняет.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def parse_fragments_tsv_strict(data: bytes, total_frames: int = 0):
    """Строгой разбор fragments.tsv для импорта разметки.

    ``Workspace._read`` молча пропускает битые строки — для сохранения работы
    это терпимо, но импорт чужого архива не должен незаметно терять данные:
    здесь любая ошибка — отказ. Формат и проверки совпадают с PUT /fragments:
    UTF-8 без control-символов (NUL молча обрывает строки у C-читателей),
    заголовок start/end, целые start/end в диапазоне
    ``[0, total_frames)`` (400), пересечения (409), позиция (400).

    Возвращает ``(fragments, position, problem)``: ``problem`` — None при
    успехе, иначе ``(HTTP-статус, текст)`` и первые два элемента None.
    ``total_frames`` = 0 — число кадров неизвестно, верхняя граница не
    проверяется (как в PUT).
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, None, (400, f"fragments.tsv не в кодировке UTF-8: {e}")

    bad = _CONTROL_RE.search(text)
    if bad is not None:
        return None, None, (
            400, f"fragments.tsv: недопустимый символ {bad.group()!r} "
                 f"в разметке")

    frags: List[dict] = []
    position = 0
    data_lines: List[str] = []
    for line in io.StringIO(text):
        if line.startswith("#"):
            key, _, value = line[1:].partition("\t")
            key = key.strip()
            if key == "position":
                try:
                    position = int(value.strip())
                except ValueError:
                    return None, None, (400, f"Некорректная строка позиции: {value.strip()!r}")
            continue
        data_lines.append(line)

    if data_lines:
        try:
            reader = csv.DictReader(data_lines, delimiter="\t")
            fieldnames = reader.fieldnames or []
            if "start" not in fieldnames or "end" not in fieldnames:
                return None, None, (400, "fragments.tsv: нет колонок start/end в заголовке")
            for row in reader:
                if None in row:
                    return None, None, (
                        400, f"Лишние колонки в строке разметки: {row.get(None)!r}")
                try:
                    start, end = int(row["start"]), int(row["end"])
                except (TypeError, ValueError):
                    return None, None, (400, f"Нечисловые start/end в строке разметки: {row!r}")
                if start < 0 or start > end or (total_frames > 0 and end >= total_frames):
                    return None, None, (
                        400, f"Фрагмент {start}-{end} вне диапазона [0, {total_frames})")
                frags.append({
                    "start": start,
                    "end": end,
                    "comment": row.get("comment") or "",
                })
        except csv.Error as e:  # NUL/битые кавычки в потоке CSV
            return None, None, (400, f"fragments.tsv не разбирается как TSV: {e}")

    if position < 0 or (total_frames > 0 and position >= total_frames):
        return None, None, (400, f"Позиция {position} вне диапазона [0, {total_frames})")

    frags.sort(key=lambda x: (x["start"], x["end"]))
    for i in range(1, len(frags)):
        if frags[i]["start"] <= frags[i - 1]["end"]:
            return None, None, (409, "Фрагменты пересекаются")
    return frags, position, None


# ─── Глобальный реестр workspace-ов (ленивое сканирование) ──────────────────

_workspaces: dict[str, Workspace] = {}
_workspaces_lock = threading.Lock()


# Ошибки фоновой валидации задач: имя -> (текст, путь файла, отпечаток
# содержимого). Выставляет асинхронная проверка после создания; список
# показывает такую задачу broken-записью (а не роняет весь список и не
# прячет задачу молча).
# Путь нужен, чтобы сказать пользователю, КАКОЙ файл не читается, и
# чтобы ошибка была привязана к файлу, а не к имени задачи: отпечаток
# содержимого протухает вместе с файлом — починили (заменили) файл, и
# ошибка про него больше не действует (правда — на диске, не в памяти).
# Чистится при успехе, удалении и переименовании (переезд ключа).
# Чисто память обвязки.
_WORKSPACE_ERRORS: Dict[str, Tuple[str, str, Optional[Tuple[int, int]]]] = {}
_WORKSPACE_ERRORS_LOCK = threading.Lock()


def note_workspace_error(name: str, message: str, path: str = "") -> None:
    """Запомнить ошибку задачи (показывается broken-записью).

    ``path`` — файл, о котором ошибка (если ошибка про конкретный файл):
    по нему выводится роль («источник»/«превью») для подсветки в UI.
    """
    stamp = frame_provider.content_stamp(path) if path else None
    with _WORKSPACE_ERRORS_LOCK:
        _WORKSPACE_ERRORS[name] = (message, path, stamp)


def clear_workspace_error(name: str) -> None:
    """Снять ошибку задачи (успех/удаление)."""
    with _WORKSPACE_ERRORS_LOCK:
        _WORKSPACE_ERRORS.pop(name, None)


def clear_all_workspace_errors() -> None:
    """Сбросить все ошибки (изоляция тестов)."""
    with _WORKSPACE_ERRORS_LOCK:
        _WORKSPACE_ERRORS.clear()


def rename_workspace_error(old: str, new: str) -> None:
    """Переезд ошибки за переименованием задачи."""
    with _WORKSPACE_ERRORS_LOCK:
        if old in _WORKSPACE_ERRORS:
            _WORKSPACE_ERRORS[new] = _WORKSPACE_ERRORS.pop(old)


def _live_error(name: str) -> Optional[Tuple[str, str]]:
    """Ошибка задачи, если она ещё про текущие файлы (None — протухла).

    Протухает, когда файл, о котором ошибка, заменили или убрали: память
    не должна помнить поломку, которой на диске уже нет.
    """
    with _WORKSPACE_ERRORS_LOCK:
        rec = _WORKSPACE_ERRORS.get(name)
    if rec is None:
        return None
    message, path, stamp = rec
    if path and stamp != frame_provider.content_stamp(path):
        return None
    return message, path


def workspace_error(name: str) -> Optional[str]:
    """Текст ошибки задачи (None — нет)."""
    rec = _live_error(name)
    return rec[0] if rec is not None else None


def workspace_error_path(name: str) -> str:
    """Путь файла, о котором записана ошибка (пусто — ошибка не про файл)."""
    rec = _live_error(name)
    return rec[1] if rec is not None else ""


# Недозалитые workspace-ы (идёт streaming тела запроса): имя -> отметка
# времени. Сканнер прячет свежие; зависшие (>30 мин, напр. после падения
# процесса) показывает как есть — их видно и можно удалить. Следов на
# диске не остаётся (чисто память обвязки).
_creating: Dict[str, float] = {}
_creating_lock = threading.Lock()
_STALE_CREATING_S = 30 * 60


def note_creating(name: str) -> None:
    """Пометить задачу как недозалитую (начало streaming)."""
    with _creating_lock:
        _creating[name] = time.time()


def clear_creating(name: str) -> None:
    """Снять пометку (поток записан, дальше — видимый indexing)."""
    with _creating_lock:
        _creating.pop(name, None)


def _is_creating(name: str) -> bool:
    """Задача в процессе заливки: прячем из списка."""
    with _creating_lock:
        ts = _creating.get(name)
    return ts is not None and (time.time() - ts) < _STALE_CREATING_S


def scan_workspaces(root: str | None = None) -> dict[str, Workspace]:
    """Сканирует root и возвращает {name: Workspace} для всех поддиректорий.

    Папка реестра проектов (``projects/``) — не задача, в выдачу не идёт;
    первый проход по установке прогоняет разовую миграцию старых задач
    в проект ``default`` (см. project_meta.ensure_migrated).
    """
    root = root or WORKSPACE_ROOT
    result: dict[str, Workspace] = {}

    if not os.path.isdir(root):
        return result

    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full) and not entry.startswith(".") \
                and entry != project_meta.PROJECTS_DIRNAME \
                and not _is_creating(entry):
            result[entry] = Workspace(name=entry, path=full)

    with _workspaces_lock:
        _workspaces.clear()
        _workspaces.update(result)

    project_meta.ensure_migrated(root)

    return result


def get_workspace(name: str) -> Optional[Workspace]:
    """Возвращает workspace по имени (или None)."""
    with _workspaces_lock:
        if name in _workspaces:
            return _workspaces[name]

    # Если не найден — пробуем обновить список.
    scan_workspaces()
    with _workspaces_lock:
        return _workspaces.get(name)


def _workspace_updated_at(ws: Workspace) -> float:
    """Время последней работы: mtime fragments.tsv, иначе mtime папки."""
    tsv = ws.fragments_path()
    try:
        if os.path.isfile(tsv):
            return os.path.getmtime(tsv)
        return os.path.getmtime(ws.path)
    except OSError:
        return 0.0


def bump_updated_at(ws: Workspace) -> None:
    """Делает workspace «свежим» (верх списка) после операций с ролями."""
    try:
        os.utime(ws.fragments_path(), None)
    except OSError:
        try:
            os.utime(ws.path, None)
        except OSError:
            pass


def _video_ver(ws: Workspace) -> str:
    """Токен версии видеофайлов: меняется при замене/пересоздании файлов.

    Собирается из (имя, размер, mtime_ns) source/preview — браузер кэширует
    кадры по URL, поэтому URL обязан меняться, когда файлы на диске изменились
    (пересоздание задачи под тем же именем, замена роли и т.д.).
    """
    parts = []
    for path in sorted(p for p in (ws.original, ws.visualization) if p):
        try:
            st = os.stat(path)
            parts.append(f"{os.path.basename(path)}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(os.path.basename(path))
    return "|".join(parts) or "none"


def _pair_warning(source: dict, preview: dict, both: bool) -> str:
    """Текст предупреждения о проблемах пары source/preview (или "").

    Пара не блокируется: рассинхрон числа кадров или пропущенные битые
    кадры показываются пользователю, решение — за ним.
    """
    parts: List[str] = []
    if source.get("skipped_frames"):
        parts.append(
            f"в источнике пропущено {source['skipped_frames']} битых кадров"
        )
    if preview.get("skipped_frames"):
        parts.append(
            f"в превью пропущено {preview['skipped_frames']} битых кадров"
        )
    if both and source["total_frames"] != preview["total_frames"]:
        parts.append(
            f"число видимых кадров не совпадает: "
            f"источник {source['total_frames']}, превью {preview['total_frames']}"
        )
    return "; ".join(parts)


def _empty_meta() -> dict:
    """Нулевые метрики (нет видео / индекс ещё строится)."""
    return {
        "total_frames": 0, "packet_frames": 0, "skipped_frames": 0,
        "width": 0, "height": 0, "fps": 0.0,
    }


def _task_dates(ws: Workspace):
    """(created_at, last_opened_at, project_id) из task.json (None — нет)."""
    try:
        meta = task_meta.load(ws.path)
    except Exception:
        return None, None, None
    return (meta.get("created_at"), meta.get("last_opened_at"),
            project_meta.resolve_pid(meta.get("project_id")))


def _task_display_name(ws: Workspace) -> str:
    """Отображаемое имя задачи (как task.name в CVAT): из task.json,
    пусто — id (имя папки). Дубли разрешены, уникален только id."""
    try:
        name = task_meta.load(ws.path).get("name") or ""
    except Exception:
        return ws.name
    return name if isinstance(name, str) and name else ws.name


def _export_outputs_present(ws_name: str, files) -> bool:
    """Все файлы записи на месте и ненулевые (пустой список — вакуумно да).

    Ручной снос нарезок мимо API виден только так: sidecar при этом может
    остаться (хеш-то совпадает — он про границы и видео, а не про наличие
    файлов), поэтому сверяемся с самими файлами, а не с ним.
    """
    out_dir = os.path.join(WORKSPACE_ROOT, ws_name, "exports")
    for item in files or []:
        name = item.get("filename", "") if isinstance(item, dict) else item
        full = os.path.join(out_dir, os.path.basename(name or ""))
        try:
            if not os.path.isfile(full) or os.path.getsize(full) == 0:
                return False
        except OSError:
            return False
    return True


def _export_state(name: str):
    """Состояние фона экспорта для бейджа списка (None — тихо).

    done из памяти сверяется с диском: нарезки, снесённые вручную мимо
    API, иначе светились бы «готовыми» до рестарта. Протухшая запись
    выкидывается из памяти. Не-done состояния ведёт фон — сверять нечего.
    Проверка только для done-задач (редкость), а не для всех строк.
    """
    try:
        from vc_fragments.views import (
            EXPORTS, EXPORTS_LOCK, _task_hash as _disk_hash,
            _video_identity as _disk_video,
        )
    except Exception:
        # Деградация: без views ни память, ни свежесть недоступны.
        return None
    with EXPORTS_LOCK:
        exp = EXPORTS.get(name)
    if exp is None:
        return None
    badge = {"state": exp.get("state"), "index": exp.get("index", 0),
             "total": exp.get("total", 1), "error": exp.get("error", "")}
    if badge["state"] != "done":
        return badge
    ws = get_workspace(name)
    if ws is None or not _export_outputs_present(name, exp.get("files")):
        with EXPORTS_LOCK:
            EXPORTS.pop(name, None)
        return None
    # Предмет экспорта цел: непустая разметка и видео на месте. Хеш
    # сверяем, только если запись его несёт (без хеша — служебные done
    # без файлов, их честность — файлами выше).
    frags = ws.load_fragments()
    vid = _disk_video(ws.original)
    if not frags or vid is None:
        with EXPORTS_LOCK:
            EXPORTS.pop(name, None)
        return None
    if "hash" in exp:
        want = _disk_hash(frags, vid["size"], vid["mtime_ns"])
        if want != exp["hash"]:
            with EXPORTS_LOCK:
                EXPORTS.pop(name, None)
            return None
    return badge


def _fast_role_meta(ws: Workspace, role: str):
    """(meta, ready): метаданные роли без долгого ожидания индекса.

    Полный проход demux+decode здесь не выполняется: если быстрого пути
    (in-memory/дисковый кэш) нет — запускается фоновая сборка, а запись
    помечается ``indexing``.
    """
    if role == "source":
        path = ws.original
    else:
        path = ws.visualization or ws.original
    if not path or not os.path.isfile(path):
        return _empty_meta(), True
    meta = frame_provider.try_get_metadata(path)
    if meta is None:
        err = frame_provider.index_error(path)
        if err is not None:
            raise ValueError(f"индекс не построен: {err}")
        return _empty_meta(), False
    return meta, True


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _index_state(ws) -> Tuple[dict, dict, bool, Optional[float]]:
    """Меты ролей + (indexing, progress): только пробы кэша, без разметки.

    Общее для полной записи и лёгкого статуса: прогресс — минимум
    по неготовым ролям; None — сборка ещё не отметилась (пульс без %).

    Обе роли опрашиваются даже если одна битая: иначе сборка индекса
    второй роли не запустится и поломка «прячется» (в списке осталось бы
    «сломан только превью», хотя сломаны оба файла).
    """
    broken = None
    try:
        preview, preview_ready = _fast_role_meta(ws, "preview")
    except ValueError as e:
        preview, preview_ready = _empty_meta(), False
        broken = e
    try:
        source, source_ready = _fast_role_meta(ws, "source")
    except ValueError as e:
        source, source_ready = _empty_meta(), False
        broken = broken or e
    indexing = not (preview_ready and source_ready)
    progress = None
    if indexing:
        vals = []
        for path, ready in (
            (ws.original, source_ready),
            (ws.visualization or ws.original, preview_ready),
        ):
            if not ready and path:
                p = frame_provider.index_progress(path)
                if p is not None:
                    vals.append(p)
        progress = min(vals) if vals else None
    if broken is not None:
        raise broken
    return preview, source, indexing, progress


def _probe_roles(ws: Workspace) -> None:
    """Запустить фоновую сборку индекса для обеих ролей (без ожидания).

    Нужно там, где запись уже признана битой: без пробы второй роли её
    поломка не попадёт в реестр ошибок индекса, и список знал бы только
    о первом сбойном файле («сломан источник», хотя сломаны оба).
    """
    try:
        _index_state(ws)
    except ValueError:
        pass


def workspace_status_entry(ws: Workspace) -> dict:
    """Волатильное состояние задачи для дешёвого опроса (тик без TSV).

    Только то, что меняется фоном: готовность индекса, прогресс, экспорт,
    mtime (маркер чужих правок разметки), broken. Фрагменты/позиция/метрики
    статичны в фоновых фазах — их тик больше не перечитывает и не гоняет
    по сети; клиент мержит это в нарисованные строки, полный список —
    на mount/возврат/CRUD и при смене состава.
    """
    err = workspace_error(ws.name)
    if err is not None:
        _probe_roles(ws)
        return {"id": ws.name, "indexing": False, "indexing_progress": None,
                "export": _export_state(ws.name),
                "updated_at": _workspace_updated_at(ws),
                "broken": True, "error": str(err)[:300],
                "error_role": broken_role(ws) or None}
    try:
        _, _, indexing, progress = _index_state(ws)
    except ValueError as e:
        return {"id": ws.name, "indexing": False, "indexing_progress": None,
                "export": _export_state(ws.name),
                "updated_at": _workspace_updated_at(ws),
                "broken": True, "error": f"{type(e).__name__}: {e}"[:300],
                "error_role": broken_role(ws) or None}
    return {"id": ws.name, "indexing": indexing, "indexing_progress": progress,
            "export": _export_state(ws.name),
            "updated_at": _workspace_updated_at(ws),
            "broken": False, "error": "", "error_role": None}


def list_workspaces_status() -> List[dict]:
    """Лёгкий снимок всех задач (см. workspace_status_entry)."""
    ws_map = scan_workspaces()
    return [workspace_status_entry(ws) for ws in ws_map.values()]


def _pair_entry(ws: Workspace, fast: bool = False) -> dict:
    """Элемент списка/деталей: роли, превью-метрики, фрагменты, позиция.

    Помимо метрик превью (таймлайн) отдаём метрики источника и признак
    согласованности пары — чтобы UI показывал, если source и preview
    разошлись по числу видимых кадров или содержат битые кадры.

    ``fast=True`` — режим списка: без долгого ожидания индекса (записи
    с незавершённой индексацией помечаются ``indexing``).

    Записанная ошибка валидации (см. note_workspace_error) бьёт запись
    сразу — список превращает её в broken через свой try/except.
    """
    err = workspace_error(ws.name)
    if err is not None:
        _probe_roles(ws)
        raise ValueError(err)
    if fast:
        preview, source, indexing, indexing_progress = _index_state(ws)
    else:
        preview = ws.metadata()  # превью (или исходник, если пары нет)
        source = ws.video_metadata("source")
        indexing = False
        indexing_progress = None
    both = bool(ws.original and ws.visualization and ws.original != ws.visualization)
    unassigned = ws.unassigned
    quality, scale = ws.load_settings()
    created_at, last_opened_at, project_id = _task_dates(ws)
    return {
        "id": ws.name,
        "name": _safe(lambda: _task_display_name(ws), ws.name),
        "project_id": project_id,
        "source_name": os.path.basename(ws.original) if ws.original else "",
        "preview_name": os.path.basename(ws.visualization) if ws.visualization else "",
        "unassigned_name": os.path.basename(unassigned[0]) if unassigned else None,
        "total_frames": preview["total_frames"],
        "width": preview["width"],
        "height": preview["height"],
        "fps": preview["fps"],
        "source_frames": source["total_frames"],
        "preview_frames": preview["total_frames"],
        "source_skipped": source.get("skipped_frames", 0),
        "preview_skipped": preview.get("skipped_frames", 0),
        "visible_match": (not both) or source["total_frames"] == preview["total_frames"],
        "pair_warning": _pair_warning(source, preview, both),
        "fragments": ws.load_fragments(),
        "position": ws.load_position(),
        "video_ver": _video_ver(ws),
        "quality": quality,
        "scale": scale,
        "updated_at": _workspace_updated_at(ws),
        "created_at": created_at,
        "last_opened_at": last_opened_at,
        "export": _export_state(ws.name),
        "indexing": indexing,
        "indexing_progress": indexing_progress,
        "broken": False,
        "error": "",
        "error_role": None,
    }


def _broken_entry(ws: Workspace, err: Exception) -> dict:
    """Запись-заглушка для битой задачи: список переживает её наличие."""
    original = _safe(lambda: os.path.basename(ws.original) if ws.original else "", "")
    visualization = _safe(
        lambda: os.path.basename(ws.visualization) if ws.visualization else "", "")
    unassigned = _safe(ws.unassigned, [])
    created_at, last_opened_at, project_id = _safe(
        lambda: _task_dates(ws), (None, None, None))
    return {
        "id": ws.name,
        "name": _safe(lambda: _task_display_name(ws), ws.name),
        "project_id": project_id,
        "source_name": original,
        "preview_name": visualization,
        "unassigned_name": os.path.basename(unassigned[0]) if unassigned else None,
        "total_frames": 0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "source_frames": 0,
        "preview_frames": 0,
        "source_skipped": 0,
        "preview_skipped": 0,
        "visible_match": True,
        "pair_warning": "",
        "fragments": _safe(ws.load_fragments, []),
        "position": _safe(ws.load_position, 0),
        "video_ver": _safe(lambda: _video_ver(ws), ""),
        "quality": DEFAULT_QUALITY,
        "scale": DEFAULT_SCALE,
        "updated_at": _workspace_updated_at(ws),
        "created_at": created_at,
        "last_opened_at": last_opened_at,
        "export": _export_state(ws.name),
        "indexing": False,
        "indexing_progress": None,
        "broken": True,
        "error": f"{type(err).__name__}: {err}"[:300],
        "error_role": broken_role(ws) or None,
    }


def list_workspaces(fast: bool = True) -> List[dict]:
    """Возвращает список workspace-ов с фрагментами, позицией и временем правки.

    Сортировка — по `updated_at` (свежее вверху): превью статус-бара в списке
    и порядок «недавние сверху» опираются на эти же данные.

    ``fast=True`` (по умолчанию) — без долгого ожидания индекса; одна битая
    задача не роняет весь список (возвращается записью с ``broken``).
    """
    ws_map = scan_workspaces()
    result = []
    for ws in ws_map.values():
        try:
            result.append(_pair_entry(ws, fast=fast))
        except Exception as e:  # noqa: BLE001 — изоляция битых записей
            logger.warning("workspace %s skipped: %r", ws.name, e)
            result.append(_broken_entry(ws, e))
    result.sort(key=lambda w: w["updated_at"], reverse=True)
    return result


def get_workspace_detail(name: str, wait_s: float = 10.0) -> Optional[dict]:
    """Возвращает полный detail workspace (с фрагментами).

    Мелкие файлы успевают за wait_s — ответ как раньше, полный. Большой
    строящийся индекс не держит запрос минутами: отдаём fast-запись
    (indexing, без блокировки), редактор ждёт готовности опросом.
    Записанная ошибка — broken-запись (не 500).
    """
    ws = get_workspace(name)
    if ws is None:
        return None
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            fast_entry = _pair_entry(ws, fast=True)
        except Exception as e:  # noqa: BLE001 — изоляция битых записей
            logger.warning("workspace %s detail skipped: %r", ws.name, e)
            return _broken_entry(ws, e)
        if not fast_entry.get("indexing") or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if fast_entry.get("indexing"):
        return fast_entry
    try:
        return _pair_entry(ws)
    except Exception as e:  # noqa: BLE001 — изоляция битых записей
        logger.warning("workspace %s detail skipped: %r", ws.name, e)
        return _broken_entry(ws, e)
