# -*- coding: utf-8 -*-
"""Права доступа к экспортированным фрагментам.

Экспорт живёт в ``<workspace>/exports`` и должен читаться/копироваться
пользователями хоста, которые не являются владельцем процесса. Модель —
без UID/GID и chown: владелец root, каталог ``0755``, готовые файлы и
sidecar ``0644``. Предки workspace получают только проход (``0711``, если
каталог был закрыт): пройти к exports можно, перечислить содержимое
workspace — нет.

Незавершённые файлы (скрытое имя с ``_fragment_``) остаются ``0600``:
чужие их не читают, пока экспорт не проверил и не опубликовал файл.

Ручная починка старых экспортов::

    python -m export_access --fix [--root /data/workspaces]
"""

from __future__ import annotations

import argparse
import os
import stat
from typing import Optional, Sequence

DIR_MODE = 0o755
FILE_MODE = 0o644
PARTIAL_MODE = 0o600
#: Канонический режим «только проход»: rwx для владельца, x группе/чужим.
TRAVERSE_BITS = stat.S_IXGRP | stat.S_IXOTH


def _chmod(path: str, mode: int) -> None:
    """Меняет права; нечитаемый/чужой файл не должен ронять весь прогон."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_ancestors(path: str, *, root: str) -> None:
    """Добавляет проход (``0711``) каталогам от ``path`` до ``root``.

    Только execute, без read: чужие проходят внутрь, но не листают
    промежуточные каталоги. Уже выданные права не сужаются.
    """
    current = os.path.abspath(path)
    stop = os.path.abspath(root)
    while True:
        try:
            st = os.stat(current)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode):
            mode = stat.S_IMODE(st.st_mode)
            need = TRAVERSE_BITS & ~mode
            if need:
                _chmod(current, mode | need)
        if current == stop:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def ensure_access(ws_path: str, *, root: str) -> None:
    """Готовит exports к чтению чужими: каталог 0755, предки с ``o+x``."""
    out_dir = os.path.join(ws_path, "exports")
    os.makedirs(out_dir, exist_ok=True)
    ensure_ancestors(ws_path, root=root)
    _chmod(out_dir, DIR_MODE)


def _is_partial(name: str) -> bool:
    """Скрытый незавершённый файл экспорта (до publish)."""
    return name.startswith(".") and "_fragment_" in name


def repair_exports(root: str) -> int:
    """Идемпотентно чинит права всех ``exports/`` под ``root``.

    Возвращает число найденных каталогов экспорта. Симлинки не трогаем:
    чужой symlink может указывать на оригинал, менять его права нельзя.
    """
    root = os.path.abspath(root)
    found = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != "exports":
            continue
        found += 1
        ensure_access(os.path.dirname(dirpath), root=root)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            _chmod(full, PARTIAL_MODE if _is_partial(name) else FILE_MODE)
    return found


def _default_root() -> str:
    """Корень workspace: env как у backend, иначе локальная папка."""
    return os.environ.get("VC_WORKSPACE_ROOT") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "workspaces")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Права доступа к экспортированным фрагментам")
    parser.add_argument("--fix", action="store_true",
                        help="починить права exports под --root")
    parser.add_argument("--root", default=_default_root(),
                        help="корень workspace (по умолчанию VC_WORKSPACE_ROOT)")
    args = parser.parse_args(argv)
    if not args.fix:
        parser.error("укажите --fix")
    repaired = repair_exports(args.root)
    print(f"exports repaired: {repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
