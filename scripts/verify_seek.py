"""Этап A: верификация точности произвольного позиционирования в OpenCV.

Проверяет гипотезу: кадры, получаемые через cap.set(CAP_PROP_POS_FRAMES, i)
(тот же путь, что использует frame_provider), побитово совпадают с кадрами,
полученными последовательным декодированием от начала видео.

Использование:
    python3 scripts/verify_seek.py [путь|каталог ...] [--step N] [--limit N]

Вердикт — строгое побитовое равенство BGR-кадров. Расхождение даже в 1 байт
считается FAIL. Для диагностики причин расхождения seek-кадр дополнительно
сравнивается с кадрами i-1 и i+1 (детект «сдвига на 1»).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import List, Optional, Tuple

import cv2

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".ts"}


def list_videos(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path]
    found = []
    for root, _dirs, files in os.walk(path):
        for f in files:
            if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                found.append(os.path.join(root, f))
    return sorted(found)


def probe(cap: cv2.VideoCapture) -> Tuple[int, int, float]:
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    return total, (width, height) if isinstance(width, int) else (0, 0), fps


def frame_digest(frame) -> Optional[bytes]:
    if frame is None:
        return None
    return hashlib.sha256(frame.tobytes()).digest()


def read_sequential(cap: cv2.VideoCapture, n: int) -> List[Optional[bytes]]:
    """Последовательно читает первые n кадров, возвращая их дайджесты."""
    digests: List[Optional[bytes]] = []
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            digests.append(None)
            break
        digests.append(frame_digest(frame))
    return digests


def seek_read(cap: cv2.VideoCapture, index: int) -> Optional[bytes]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    if not ok:
        return None
    return frame_digest(frame)


def duration(hours: float) -> str:
    h = int(hours // 3600)
    m = int((hours % 3600) // 60)
    s = int(hours % 60)
    return f"{h}:{m:02d}:{s:02d}"


def verify_file(
    path: str,
    step: int,
    limit: int,
    tail_probe: int,
    verbose: bool,
) -> Tuple[bool, dict]:
    """Проверяет один файл. Возвращает (passed, статистика)."""
    ref_cap = cv2.VideoCapture(path)
    if not ref_cap.isOpened():
        return False, {"error": "не удалось открыть файл"}

    total, (w, h), fps_guess = probe(ref_cap)
    fps = fps_guess if fps_guess and fps_guess > 0 else 0.0

    # Сколько кадров реально читается последовательно (эталон).
    n_seq = min(limit if limit else (total if total > 0 else 0), total if total > 0 else int(1e9))
    if total <= 0:
        # Нет доверия к frame_count — прочитаем сколько выйдет.
        n_seq = limit or 1000

    ref_digests: List[Optional[bytes]] = read_sequential(ref_cap, n_seq)
    n_read = len(ref_digests)

    # Индексы, которые проверяем: каждый step-й кадр, но минимум первые/последние.
    indices = set(range(0, n_read, max(1, step)))
    for edge in (min(5, n_read), n_read - 1 if n_read else 0):
        if 0 <= edge < n_read:
            indices.add(edge)

    # Поэтапно: для больших видео эталон-чтение обрываем, и сэмпл смещается.
    # seek-проверка идёт ОТДЕЛЬНЫМ capture (чтобы не влияло на sequential).
    mis: List[dict] = []
    test_cap = cv2.VideoCapture(path)
    for i in sorted(indices):
        d_test = seek_read(test_cap, i)
        d_ref = ref_digests[i] if i < len(ref_digests) else None
        if d_test == d_ref:
            continue
        # Диагностика: сдвиг на 1 назад/вперёд?
        kind = "другой кадр"
        if i > 0 and ref_digests[i - 1] == d_test:
            kind = "сдвиг на 1 (seek = i-1)"
        elif i + 1 < len(ref_digests) and ref_digests[i + 1] == d_test:
            kind = "сдвиг на 1 (seek = i+1)"
        mis.append({"index": i, "kind": kind})
    test_cap.release()
    ref_cap.release()

    passed = not mis
    return passed, {
        "path": path,
        "frames_total": total,
        "frames_read_seq": n_read,
        "frames_checked": len(indices),
        "w": w,
        "h": h,
        "fps": round(fps, 3),
        "duration": duration(n_read / fps) if fps else "?",
        "mismatches": mis[:50],
        "mismatch_count": len(mis),
        "passed": passed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="видеофайл(ы) или каталог(и)")
    ap.add_argument("--step", type=int, default=1, help="проверять каждый N-й кадр")
    ap.add_argument("--limit", type=int, default=0, help="макс. кадров эталонного чтения (0 = все)")
    ap.add_argument("--tail-probe", type=int, default=100, help="не используется (см. README)")
    ap.add_argument("--verbose", action="store_true", help="вывести все расхождения")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        files.extend(list_videos(p))
    if not files:
        print("Видео не найдено", file=sys.stderr)
        return 2

    all_ok = True
    for path in files:
        passed, stat = verify_file(
            path, args.step, args.limit, args.tail_probe, args.verbose
        )
        if "error" in stat:
            print(f"FAIL  {stat['error']:20} {path}")
            all_ok = False
            continue
        dur = stat["duration"]
        verdict = "PASS" if passed else "FAIL"
        mc = stat["mismatch_count"]
        print(
            f"{verdict:4} checked={stat['frames_checked']:6} seq={stat['frames_read_seq']:7} "
            f"total={stat['frames_total']:7} {stat['w']}x{stat['h']} {stat['fps']}fps "
            f"dur={dur} mism={mc:4}  {path}"
        )
        if passed:
            all_ok = all_ok and True
        else:
            all_ok = False
            for m in stat["mismatches"][:50]:
                print(f"     #{m['index']}: {m['kind']}")
            if mc > 50:
                print(f"     ...ещё {mc - 50} расхождений")

    print("\nИтог:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())