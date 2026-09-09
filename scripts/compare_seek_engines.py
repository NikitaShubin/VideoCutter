"""Этап C, часть 1: PyAV vs OpenCV — точность seek и всеядность.

Гипотеза: OpenCV трещит по seek (сдвиг на 1 у H.264 c B-frames/VFR,
NRHT AV1 не декодирует). Если PyAV (обёртка над FFmpeg) даёт точный
побитовый seek и читает AV1 — он может заменить cv2 как движок
покадрового доступа.

Метод (честный, по PTS):
  1. Последовательный прогон PyAV: для каждого кадра запоминаем дайджест и
     его PTS. Это эталон.
  2. Для каждого проверяемого индекса i: seek к PTS кадра i - 1 (чуть раньше),
     декодируем до первого кадра с PTS >= PTS(i), сверяем побитово с эталоном.
  3. То же для OpenCV (cap.set CAP_PROP_POS_FRAMES), сверка с его сваим
     эталоном (тот же файл).

Все диэти сравненить по одной нумерации (0-based, порядок декодирования).

Использование:
    /tmp/vcvenv/bin/python scripts/compare_seek_engines.py ФАЙЛ... [--step N]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import List, Optional, Tuple

import av
import cv2

FRAME_FORMAT = "bgr24"


def seq_pyav(path: str, limit: int):
    """Последовательный проход PyAV: списки (time_s, digest)."""
    container = av.open(path)
    stream = container.streams.video[0]
    pts_list: List[float] = []
    dig_list: List[bytes] = []
    for frame in container.decode(stream):
        img = frame.to_ndarray(format=FRAME_FORMAT)
        dig_list.append(hashlib.sha256(img.tobytes()).digest())
        tb = frame.time_base or stream.time_base
        pts_list.append((frame.pts or 0) * tb)
        if limit and len(dig_list) >= limit:
            break
    w = stream.codec_context.width
    h = stream.codec_context.height
    avg = stream.average_rate
    fps = float(avg) if avg else 0.0
    container.close()
    return pts_list, dig_list, w, h, fps


def seek_pyav(stream, container, target_s: float) -> Optional[bytes]:
    """Декодирует первый кадр с PTS >= target_s после seek чуть раньше."""
    tb = stream.time_base
    if tb is None or tb.denominator == 0:
        raise ValueError("нет time_base")
    # Идём на ~1,5 кадра раньше, чтобы гарантированно начать до цели.
    offset = int((target_s - 0.05) / tb) if target_s > 0.05 else 0
    container.seek(offset, stream=stream, backward=True, any_frame=False)
    for frame in container.decode(stream):
        f_s = (frame.pts or 0) * (frame.time_base or tb)
        if f_s >= target_s:
            return hashlib.sha256(frame.to_ndarray(format=FRAME_FORMAT).tobytes()).digest()
    return None


def seq_cv2(path: str, limit: int):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None
    dig_list: List[bytes] = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        dig_list.append(hashlib.sha256(frame.tobytes()).digest())
        if limit and len(dig_list) >= limit:
            break
    cap.release()
    return dig_list


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--step", type=int, default=200)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files: List[str] = []
    for p in args.paths:
        if os.path.isfile(p):
            files.append(p)
        else:
            for root, _ds, fs in os.walk(p):
                for f in fs:
                    if os.path.splitext(f)[1].lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
                        files.append(os.path.join(root, f))
    for path in sorted(files):
        print("=====", path)
        pts_list, dig_list, w, h, fps = seq_pyav(path, args.limit)
        base = os.path.basename(path)
        print(f"  pyav seq: {len(dig_list)} frames, {w}x{h}, {fps:.3f} fps")
        if not pts_list:
            print("  pyav: не удалось декодировать ни одного кадра")
            continue
        n_read = len(dig_list)

        # Индексы для проверки.
        indices = set(range(0, n_read, max(1, args.step)))
        for edge in (0, n_read - 1):
            edge = min(edge, n_read - 1)
            indices.add(edge)

        # --- PyAV seek ---
        container = av.open(path)
        stream = container.streams.video[0]
        pyav_mism = 0
        first_bad_report = None
        for idx in sorted(indices):
            d = seek_pyav(stream, container, pts_list[idx])
            if d != dig_list[idx]:
                pyav_mism += 1
                if first_bad_report is None:
                    # классифицируем: равен предыдущему/следующему?
                    kind = "другой кадр"
                    if idx > 0 and d == dig_list[idx - 1]:
                        kind = "i-1"
                    elif idx + 1 < n_read and d == dig_list[idx + 1]:
                        kind = "i+1"
                    first_bad_report = (idx, kind)
        container.close()
        print(f"  pyav seek: checked={len(indices)} mism={pyav_mism}", end="")
        if first_bad_report:
            print(f"  first bad: #{first_bad_report[0]} [{first_bad_report[1]}]")
        else:
            print()

        # --- OpenCV seek (против его же эталона) ---
        cv_dig = seq_cv2(path, args.limit)
        if cv_dig is None:
            print("  cv2 seek: файл не открывается")
            continue
        n_cv = min(len(cv_dig), n_read)
        cap = cv2.VideoCapture(path)
        cv2_mism = 0
        first_cv = None
        for idx in sorted(indices):
            if idx >= n_cv:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            d = hashlib.sha256(frame.tobytes()).digest() if ok else None
            if d != cv_dig[idx]:
                cv2_mism += 1
                if first_cv is None:
                    kind = "другой кадр"
                    if idx > 0 and d == cv_dig[idx - 1]:
                        kind = "i-1"
                    elif idx + 1 < n_cv and d == cv_dig[idx + 1]:
                        kind = "i+1"
                    first_cv = (idx, kind)
        cap.release()
        print(f"  cv2 seek: checked={len(indices)} mism={cv2_mism}", end="")
        if first_cv:
            print(f"  first bad: #{first_cv[0]} [{first_cv[1]}]")
        else:
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())