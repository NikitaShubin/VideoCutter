#!/usr/bin/env python3
"""Эмуляция десктопного PyVideoCutter: последовательный cv2.VideoCapture.

Десктоп (tools-main old engine): VideoReader предзаполняет буфер до 512
кадров, дальше играет из памяти; вперёд дочитывает по 1 кадру cap.read()
(чисто последовательное чтение, без seek). Поэтому ориентир производительности
= скорость последовательного декода всех кадров без HTTP и JPEG-энкода.

Запускается ВНУТРИ контейнера (в образе есть cv2 + av):
    docker run --rm -v "$PWD/workspaces:/data/workspaces" ... bench_engine_reference.py
"""
from __future__ import annotations

import av
import cv2
import sys
import time
from pathlib import Path

ROOT = Path("/data/workspaces")

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".m2ts", ".ts"}


def videos():
    out = []
    for ws in sorted(p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for f in sorted(p for p in ws.iterdir() if p.suffix.lower() in VIDEO_EXTS):
            out.append((str(ws.relative_to(ROOT)), f.name, str(f)))
    return out


def seq_time(path: str) -> tuple[float, int, int, int]:
    """Последовательный cap.read() всех кадров. Возвращает (сек, w, h, n)."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"не открылся: {path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    t0 = time.perf_counter()
    n = 0
    while True:
        ok, _ = cap.read()
        if not ok:
            break
        n += 1
    dt = time.perf_counter() - t0
    cap.release()
    return dt, w, h, n


def main():
    print(f"{'workspace':<60} {'file':<40} {'W':>5} {'H':>5} {'N':>5} "
          f"{'T_seq':>9} {'метрика':>12} {'кадр/с':>8}")
    print("-" * 150)
    for ws, name, path in videos():
        dt, w, h, n = seq_time(path)
        metric = dt / (w * h * n)
        fps = n / dt
        print(f"{ws:<60} {name:<40} {w:>5} {h:>5} {n:>5} "
              f"{dt:>8.2f}s {metric:>10.9f} {fps:>7.1f}")
    ffmpeg_ref()


def ffmpeg_ref():
    """Альтернативный ориентир: чистая скорость декода через PyAV (без cv2)."""
    print("\nPyAV последовательный декод:")
    for ws, name, path in videos():
        cont = av.open(path)
        stream = cont.streams.video[0]
        t0 = time.perf_counter()
        n = 0
        for fr in cont.decode(stream):
            n += 1
        dt = time.perf_counter() - t0
        cont.close()
        w, h = stream.codec_context.width, stream.codec_context.height
        metric = dt / (w * h * n)
        print(f"{ws:<60} {name:<40} {w:>5} {h:>5} {n:>5} "
              f"{dt:>8.2f}s {metric:>10.9f} {n / dt:>7.1f}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--ffmpeg":
        ffmpeg_ref()
    else:
        main()