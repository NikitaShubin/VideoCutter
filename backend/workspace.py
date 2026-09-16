# -*- coding: utf-8 -*-
"""Stateless workspace: сканер директорий + TSV-хранилище фрагментов.

Workspace — это папка на диске с видеофайлами и fragments.tsv.
Никакой БД, никакого ORM. Метаданные вычисляются на лету через PyAV.
"""

from __future__ import annotations

import csv
import os
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from vc_pairs import frame_provider
from videocutter.standalone import workspace as ws_fs
from videocutter.standalone.workspace import VIDEO_EXTS

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
    _fragments: Optional[List[dict]] = field(default=None, repr=False)
    _position: Optional[int] = field(default=None, repr=False)
    _settings: Optional[tuple] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _discover_videos(self) -> None:
        """Находит роли видео в директории: source (original), preview, unassigned.

        Роли кодируются именами файлов: унифицированные ``source.<ext>`` /
        ``preview.<ext>``, а также суффиксные ``<base>_source``/``<base>_preview``;
        старые flat-workspaces с ``_viz`` распознаются как legacy-превью.
        """
        if self._original is not None:
            return

        found = ws_fs.classify_videos(WORKSPACE_ROOT, self.name)
        self._original = found["source"] or ""
        self._visualization = found["preview"] or ""
        self._unassigned = found["unassigned"]

    def invalidate_videos(self) -> None:
        """Сбрасывает кэш ролей и метаданных (после замены/переименования)."""
        self._original = None
        self._visualization = None
        self._unassigned = None
        self._meta = None

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
        берётся из превью), а нарезаем — из source.
        """
        if self._meta is not None:
            return self._meta

        path = self.visualization or self.original
        if not path or not os.path.isfile(path):
            self._meta = {
                "total_frames": 0, "width": 0, "height": 0, "fps": 0.0,
            }
            return self._meta

        self._meta = frame_provider.get_metadata(path)
        return self._meta

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


# ─── Глобальный реестр workspace-ов (ленивое сканирование) ──────────────────

_workspaces: dict[str, Workspace] = {}
_workspaces_lock = threading.Lock()


def scan_workspaces(root: str | None = None) -> dict[str, Workspace]:
    """Сканирует root и возвращает {name: Workspace} для всех поддиректорий."""
    root = root or WORKSPACE_ROOT
    result: dict[str, Workspace] = {}

    if not os.path.isdir(root):
        return result

    for entry in sorted(os.listdir(root)):
        full = os.path.join(root, entry)
        if os.path.isdir(full) and not entry.startswith("."):
            result[entry] = Workspace(name=entry, path=full)

    with _workspaces_lock:
        _workspaces.clear()
        _workspaces.update(result)

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


def _pair_entry(ws: Workspace) -> dict:
    """Элемент списка/деталей: роли, превью-метрики, фрагменты, позиция."""
    meta = ws.metadata()
    unassigned = ws.unassigned
    quality, scale = ws.load_settings()
    return {
        "id": ws.name,
        "source_name": os.path.basename(ws.original) if ws.original else "",
        "preview_name": os.path.basename(ws.visualization) if ws.visualization else "",
        "unassigned_name": os.path.basename(unassigned[0]) if unassigned else None,
        "total_frames": meta["total_frames"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "fragments": ws.load_fragments(),
        "position": ws.load_position(),
        "video_ver": _video_ver(ws),
        "quality": quality,
        "scale": scale,
        "updated_at": _workspace_updated_at(ws),
    }


def list_workspaces() -> List[dict]:
    """Возвращает список workspace-ов с фрагментами, позицией и временем правки.

    Сортировка — по `updated_at` (свежее вверху): превью статус-бара в списке
    и порядок «недавние сверху» опираются на эти же данные.
    """
    ws_map = scan_workspaces()
    result = [_pair_entry(ws) for ws in ws_map.values()]
    result.sort(key=lambda w: w["updated_at"], reverse=True)
    return result


def get_workspace_detail(name: str) -> Optional[dict]:
    """Возвращает полный detail workspace (с фрагментами)."""
    ws = get_workspace(name)
    if ws is None:
        return None
    return _pair_entry(ws)
