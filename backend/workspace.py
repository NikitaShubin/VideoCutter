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
from pathlib import Path
from typing import List, Optional

from vc_pairs import frame_provider

# Корневая директория со всеми workspace-ами.
WORKSPACE_ROOT = os.getenv("VC_WORKSPACE_ROOT", os.path.join(os.path.dirname(__file__), "workspaces"))

# Расширения видеофайлов, которые мы распознаём.
_VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".mts"}

# Имя файла с фрагментами (аналог txt-файла в PVC).
FRAGMENTS_FILE = "fragments.tsv"


@dataclass
class Workspace:
    """Один workspace: папка с видео + fragments.tsv."""

    name: str  # Имя директории (id workspace).
    path: str  # Абсолютный путь к директории.

    # Заполняются лениво при first access.
    _original: Optional[str] = field(default=None, repr=False)
    _visualization: Optional[str] = field(default=None, repr=False)
    _meta: Optional[dict] = field(default=None, repr=False)
    _fragments: Optional[List[dict]] = field(default=None, repr=False)
    _position: Optional[int] = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _discover_videos(self) -> None:
        """Находит оригинальное и визуализационное видео в директории."""
        if self._original is not None:
            return

        videos = sorted(
            p for p in Path(self.path).iterdir()
            if p.is_file() and p.suffix.lower() in _VIDEO_EXTS
        )

        if not videos:
            self._original = ""
            self._visualization = ""
            return

        # Ищем видео с суффиксами _viz / _visualization / _vis.
        viz_suffixes = ("_viz", "_visualization", "_vis")
        original = None
        visualization = None

        for v in videos:
            stem_lower = v.stem.lower()
            if any(stem_lower.endswith(s) for s in viz_suffixes):
                visualization = str(v)
            elif original is None:
                original = str(v)

        # Если нашли только визуализацию — она и есть original.
        if original is None and visualization is not None:
            original = visualization
            visualization = None

        self._original = original or ""
        self._visualization = visualization or ""

    @property
    def original(self) -> str:
        self._discover_videos()
        return self._original

    @property
    def visualization(self) -> str:
        self._discover_videos()
        return self._visualization

    @property
    def has_visualization(self) -> bool:
        return bool(self.visualization)

    def metadata(self) -> dict:
        """Метаданные видео (вычисляется на лету через PyAV)."""
        if self._meta is not None:
            return self._meta

        path = self.original
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

        if os.path.isfile(tsv):
            with open(tsv, newline="", encoding="utf-8") as f:
                data_lines: List[str] = []
                for line in f:
                    if line.startswith("#"):
                        key, _, value = line[1:].partition("\t")
                        if key.strip() == "position":
                            try:
                                position = int(value.strip())
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
        return frags, position

    def load_fragments(self) -> List[dict]:
        """Читает fragments.tsv → список {"start": int, "end": int, "comment": str}."""
        with self._lock:
            return self._read()[0]

    def load_position(self) -> int:
        """Текущий кадр, на котором пользователь завершил работу (по умолчанию 0)."""
        with self._lock:
            return self._read()[1]

    def _write(self, fragments: List[dict], position: int) -> None:
        """Пишет fragments.tsv: строку `# position` + таблицу фрагментов."""
        tsv = self.fragments_path()
        os.makedirs(os.path.dirname(tsv) or ".", exist_ok=True)
        with open(tsv, "w", newline="", encoding="utf-8") as f:
            f.write(f"# position\t{int(position)}\n")
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
        """Сбрасывает кэш фрагментов и позиции (при внешних изменениях)."""
        self._fragments = None
        self._position = None


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


def list_workspaces() -> List[dict]:
    """Возвращает список workspace-ов (метаданные без фрагментов)."""
    ws_map = scan_workspaces()
    result = []
    for name, ws in ws_map.items():
        meta = ws.metadata()
        result.append({
            "id": name,
            "original_name": os.path.basename(ws.original) if ws.original else "",
            "visualization_name": os.path.basename(ws.visualization) if ws.visualization else "",
            "total_frames": meta["total_frames"],
            "width": meta["width"],
            "height": meta["height"],
            "fps": meta["fps"],
        })
    return result


def get_workspace_detail(name: str) -> Optional[dict]:
    """Возвращает полный detail workspace (с фрагментами)."""
    ws = get_workspace(name)
    if ws is None:
        return None

    meta = ws.metadata()
    frags = ws.load_fragments()

    return {
        "id": name,
        "original_name": os.path.basename(ws.original) if ws.original else "",
        "visualization_name": os.path.basename(ws.visualization) if ws.visualization else "",
        "total_frames": meta["total_frames"],
        "width": meta["width"],
        "height": meta["height"],
        "fps": meta["fps"],
        "fragments": frags,
        "position": ws.load_position(),
    }
