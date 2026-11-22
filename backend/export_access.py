# -*- coding: utf-8 -*-
"""Права доступа к экспортированным фрагментам.

Экспорт живёт в ``<workspace>/exports`` и должен читаться/копироваться
пользователями хоста, которые не являются владельцем процесса. Модель —
без UID/GID и chown: владелец root, каталог ``0755``, готовые файлы и
sidecar ``0644``. Предки workspace получают только проход (``0711``, если
каталог был закрыт): пройти к exports можно, перечислить содержимое
workspace — нет.

Граница контейнера: внутри виден только ``VC_WORKSPACE_ROOT`` (bind-mount).
Предки **хоста** выше точки монтирования контейнер не видит и починить не
может — для них есть ``VC_ACCESS_ANCHOR`` и запуск на хосте::

    sudo python3 backend/export_access.py --fix \
        --root <checkout>/workspaces --anchor /media/user

Если ФС игнорирует chmod (NTFS/exFAT/CIFS с ``fmask``/``dmask``), права не
применятся: ``ensure_access`` вернёт проблемы и напишет предупреждение.
Лечится опциями монтирования (``fmask=0022,dmask=0022``), а не кодом.

Незавершённые файлы (скрытое имя с ``_fragment_``) остаются ``0600``:
чужие их не читают, пока экспорт не проверил и не опубликовал файл.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
from typing import Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

DIR_MODE = 0o755
FILE_MODE = 0o644
PARTIAL_MODE = 0o600
#: Канонический режим «только проход»: rwx для владельца, x группе/чужим.
TRAVERSE_BITS = stat.S_IXGRP | stat.S_IXOTH
#: Минимум для чужого: войти в каталог и прочитать файл.
DIR_OTHER_BITS = stat.S_IROTH | stat.S_IXOTH
FILE_OTHER_BITS = stat.S_IROTH
#: Подсказка для неисправимой ФС.
FS_HINT = ("ФС игнорирует chmod (NTFS/exFAT/CIFS?). Проверьте опции "
           "монтирования: fmask=0022,dmask=0022 (или umask=0022)")


def _mode(path: str) -> Optional[int]:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _has(path: str, required: int) -> bool:
    mode = _mode(path)
    return mode is not None and (mode & required) == required


def _chmod(path: str, mode: int, required: int = 0) -> bool:
    """Меняет права и проверяет, что ФС их приняла."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return _has(path, required) if required else True


def _ancestors(path: str, root: str) -> Iterator[str]:
    """Путь и его предки до ``root`` включительно (или до корня ФС)."""
    current = os.path.abspath(path)
    stop = os.path.abspath(root)
    while True:
        yield current
        if current == stop:
            return
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _problem(path: str, required: int) -> Optional[str]:
    mode = _mode(path)
    if mode is None:
        return None
    if (mode & required) == required:
        return None
    return f"{path}: режим {oct(mode)}, нужен {oct(required)}; {FS_HINT}"


def ensure_ancestors(path: str, *, root: str) -> List[str]:
    """Добавляет проход (``0711``) каталогам от ``path`` до ``root``.

    Только execute, без read: чужие проходят внутрь, но не листают
    промежуточные каталоги. Уже выданные права не сужаются. Возвращает
    список неисправимых мест (пусто — всё хорошо).
    """
    problems: List[str] = []
    for current in _ancestors(path, root):
        try:
            st = os.stat(current)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        mode = stat.S_IMODE(st.st_mode)
        need = TRAVERSE_BITS & ~mode
        if need:
            _chmod(current, mode | need, stat.S_IXOTH)
        problem = _problem(current, stat.S_IXOTH)
        if problem:
            problems.append(problem)
    return problems


def ensure_access(ws_path: str, *, root: Optional[str] = None) -> List[str]:
    """Готовит exports к чтению чужими: каталог 0755, предки с ``o+x``.

    ``root`` — якорь обхода. По умолчанию ``VC_ACCESS_ANCHOR`` или сам
    workspace: подниматься выше точки монтирования контейнера бессмысленно.
    """
    anchor = root or os.environ.get("VC_ACCESS_ANCHOR") or ws_path
    out_dir = os.path.join(ws_path, "exports")
    os.makedirs(out_dir, exist_ok=True)
    problems = ensure_ancestors(ws_path, root=anchor)
    _chmod(out_dir, DIR_MODE, DIR_OTHER_BITS)
    problem = _problem(out_dir, DIR_OTHER_BITS)
    if problem:
        problems.append(problem)
    if problems:
        logger.warning("Права exports не применились: %s", "; ".join(problems))
    return problems


def _is_partial(name: str) -> bool:
    """Скрытый незавершённый файл экспорта (до publish)."""
    return name.startswith(".") and "_fragment_" in name


def ensure_file(path: str, *, mode: int = FILE_MODE) -> List[str]:
    """Готовит опубликованный файл к чтению чужими; возвращает проблемы.

    Идемпотентно и безопасно для ФС, игнорирующих chmod: вызывается при
    скачивании, чтобы старые нарезки чинились без повторного экспорта.
    """
    if _is_partial(os.path.basename(path)):
        return []
    if not _chmod(path, mode, FILE_OTHER_BITS):
        problem = _problem(path, FILE_OTHER_BITS)
        problems = [problem] if problem else []
        if problems:
            logger.warning("Права файла не применились: %s",
                           "; ".join(problems))
        return problems
    return []


def repair_exports(root: str, *, anchor: Optional[str] = None) -> int:
    """Идемпотентно чинит права всех ``exports/`` под ``root``.

    ``anchor`` позволяет дотянуться до предков выше ``root`` (хостовая
    установка). Возвращает число найденных каталогов экспорта. Симлинки не
    трогаем: чужой symlink может указывать на оригинал.
    """
    root = os.path.abspath(root)
    target = anchor or os.environ.get("VC_ACCESS_ANCHOR") or root
    found = 0
    problems: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != "exports":
            continue
        found += 1
        problems.extend(ensure_access(os.path.dirname(dirpath), root=target))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            want = PARTIAL_MODE if _is_partial(name) else FILE_MODE
            _chmod(full, want, FILE_OTHER_BITS if want == FILE_MODE else 0)
            problem = _problem(full, FILE_OTHER_BITS) if want == FILE_MODE \
                else None
            if problem:
                problems.append(problem)
    if problems:
        logger.warning("Починка прав завершилась с проблемами: %s",
                       "; ".join(dict.fromkeys(problems)))
    return found


def report_for_root(root: str, *,
                    anchor: Optional[str] = None) -> dict:
    """Диагностика цепочки доступа для ``GET /api/v1/cache`` (без chmod)."""
    root = os.path.abspath(root)
    target = anchor or os.environ.get("VC_ACCESS_ANCHOR") or root
    chain = []
    problems: List[str] = []
    for current in _ancestors(root, target):
        mode = _mode(current)
        chain.append({"path": current,
                      "mode": None if mode is None else oct(mode)})
        problem = _problem(current, stat.S_IXOTH)
        if problem:
            problems.append(problem)
    note = ""
    if os.path.abspath(target) == root:
        note = ("проверены только контейнерные предки; предки хоста выше "
                "точки монтирования задайте через VC_ACCESS_ANCHOR")
    return {
        "root": root,
        "anchor": target,
        "chain": chain,
        "problems": problems,
        "ok": not problems,
        "note": note,
        "hint": FS_HINT if problems else "",
    }


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
    parser.add_argument("--anchor", default=os.environ.get("VC_ACCESS_ANCHOR"),
                        help="верхний предок для обхода (хост: /media/user)")
    args = parser.parse_args(argv)
    if not args.fix:
        parser.error("укажите --fix")
    repaired = repair_exports(args.root, anchor=args.anchor)
    print(f"exports repaired: {repaired}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
