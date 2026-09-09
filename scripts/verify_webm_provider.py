#!/usr/bin/env python3
"""Боевой прогон PyAV-провайдера кадров на webm-файлах.

Для каждого файла строится полный последовательный эталон (декод от кадра 0,
без seek), затем ``frame_provider.read_frame`` сверяется на probe-точках.
BGR-побеги: храним только для probe-точек, иначе память переполнится.

Ключевая независимость: эталон считан последовательным декодом с нуля —
в нём нечему дрейфовать, в отличие от seek-пути провайдера.

Запуск из venv с av+opencv:  /tmp/vcvenv/bin/python scripts/verify_webm_provider.py
"""
from __future__ import annotations

import hashlib
import sys
import time
from typing import Dict, List

sys.path.insert(0, "backend")

import av  # noqa: E402
import numpy as np  # noqa: E402

from vc_pairs import frame_provider  # noqa: E402

SEARCH_DIR = "/d/Work/AD/DataOps/Боевые видео/"
FILES = [
    ("Карьерный трек", "Карьерный трек в AI [ozya-ekgLl0].webm"),
    ("Разбор 7 проектов", "Разбор 7 проектов по компьютерному зрению [xBBcpDZGqeE].webm"),
    ("Создание проекта по CV", "Создание реального проекта по CV [6gpvVpJ3cKA].webm"),
    ("初音ミク AV1", "【初音ミク】 細菌汚染 - Bacterial Contamination - 【3DPV】#骸音シーエ #シーエ #calneca  @calciu_bot [tktcOUi-x-A].webm"),
]
PROBE_STEPS = 20  # шаг сетки probe-точек (равномерно по файлу)


def digest(np_img: np.ndarray) -> str:
    return hashlib.sha256(np_img.tobytes()).hexdigest()[:16]


def build_sequential_reference(path: str, probe: set[int]) -> Dict[int, str]:
    """Полный последовательный декод от кадра 0; digests только для probe."""
    c = av.open(path)
    s = c.streams.video[0]
    ref: Dict[int, str] = {}
    t0 = time.perf_counter()
    total = 0
    for i, f in enumerate(c.decode(s)):
        if i in probe:
            ref[i] = digest(f.to_ndarray(format="bgr24"))
        total = i + 1
        if total % 20000 == 0:
            print(f"    ...эталон: {total} кадров, {time.perf_counter()-t0:.0f}s", flush=True)
    c.close()
    return ref, total


def run_one(name: str, rel: str) -> bool:
    path = SEARCH_DIR + rel
    print(f"\n===== {path}", flush=True)

    # Метadata первого уровня (не форсирует pts_map для файлов с frames>0).
    t0 = time.perf_counter()
    meta = frame_provider.get_metadata(path)
    dt_meta = time.perf_counter() - t0
    print(f"    get_metadata: {meta} за {dt_meta:.1f}s", flush=True)

    total = meta["total_frames"]
    if total <= 0:
        print("    FAIL: total_frames=0", flush=True)
        frame_provider.close_source(path)
        return False

    # Равномерная сетка + границы.
    probe = {0, total - 1}
    step = max(1, total // PROBE_STEPS)
    probe.update(range(step, total - step, step))
    probe = sorted(p for p in probe if 0 <= p < total)
    print(f"    probe-точек: {len(probe)} (0..{total-1}, сетка {step})", flush=True)

    t0 = time.perf_counter()
    ref, seq_total = build_sequential_reference(path, set(probe))
    dt_ref = time.perf_counter() - t0
    print(f"    эталон: {seq_total} кадров (последний idx {seq_total-1}) за {dt_ref:.0f}s", flush=True)

    cap = frame_provider._get_cap(path)
    mism: List[str] = []
    times: List[float] = []
    for idx in probe:
        t0 = time.perf_counter()
        bgr = cap.read_frame(idx)
        times.append(time.perf_counter() - t0)
        got = digest(bgr) if bgr is not None else "None"
        if got != ref[idx]:
            kind = "?"
            # Классифицируем смещение в пределах ±2 (для понимания).
            mism.append((idx, got, ref[idx]))
    cap.close()

    ok = not mism and seq_total == total
    med = sorted(times)[len(times) // 2]
    print(f"    read_frame ×{len(probe)}: bad={len(mism)} {mism[:3]} | "
          f"median={med*1000:.0f}ms max={max(times)*1000:.0f}ms", flush=True)
    print(f"    кадры: декод-эталон={seq_total}, provider-meta={total} → "
          f"{'OK' if seq_total == total else 'РАСХОЖДЕНИЕ'}", flush=True)
    print(f"    ВЕРДИКТ: {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=int, default=None, help="индекс файла из FILES")
    args = parser.parse_args()

    sel = [FILES[args.file]] if args.file is not None else FILES
    results = []
    for name, rel in sel:
        ok = run_one(name, rel)
        results.append((name, ok))
        frame_provider.close_source(SEARCH_DIR + rel)
    print("\n=================== ИТОГ ===================")
    for name, ok in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())