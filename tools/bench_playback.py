#!/usr/bin/env python3
"""Бенчмарк воспроизведения через HTTP API.

Метрика (как определил пользователь): среднее по двум направлениям время
сквозного прохождения всех кадров без пропусков, делённое на W*H*N
(сек/пиксель). Требования замера:
  * каждый кадр запрашивается отдельным запросом ровно один раз
    (quality=75, scale=1.0 — ресайз не применяется);
  * параметры_enabled прогон вперёд и назад;
  * перед каждым видео контейнер рестартуется (чистый кэш), чтобы
    «холодное» воспроизведение было честным.

Запуск (с хоста, где живёт python3 = 3.14+):
    python3 tools/bench_playback.py [--no-restart] [--url URL] [ws1@kind ws2@kind ...]
По умолчанию измеряются все workspace-ы: для задач с 2 видео (original !=
visualization) — оба файла; с одним — visualization.
"""
from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import time
from urllib.parse import quote

DEFAULT_URL = "http://localhost:8001"
QUALITY = 75
SCALE = 1.0
CONTAINER = "videocutter_backend"


def http_get_json(url_root: str, path: str, conn_store: dict):
    host, port = _split(url_root)
    conn = conn_store.get("conn")
    try:
        if conn is None:
            conn = http.client.HTTPConnection(host, port, timeout=300)
        conn.request("GET", path)
        res = conn.getresponse()
        body = res.read()
        conn_store["conn"] = conn
        return res.status, body
    except (TimeoutError, ConnectionError):
        conn_store["conn"] = None
        raise


def _split(url: str):
    url = url.rstrip("/")
    if url.startswith("http://"):
        url = url[len("http://"):]
    host, _, port = url.partition(":")
    if ":" not in url:
        port = "80"
    return host, int(port)


def list_workspaces(url: str):
    conn_store = {}
    status, body = http_get_json(url, "/api/v1/workspaces/", conn_store)
    if status != 200:
        raise SystemExit(f"GET /api/v1/workspaces/ -> HTTP {status}")
    return json.loads(body)


def frame_url(ws_id: str, index: int, kind: str) -> str:
    q = f"?video={quote(kind)}&quality={QUALITY}&scale={SCALE}"
    return f"/api/v1/workspaces/{quote(ws_id)}/frame/{index}/{q}"


def one_direction(url: str, ws_id: str, kind: str, frames, total: int) -> tuple[float, int]:
    """Сквозной прогон всех кадров без пропусков. Возвращает (сек, число 400..499)."""
    conn_store = {}
    t0 = time.perf_counter()
    failed = 0
    for i in frames:
        status, body = http_get_json(url, frame_url(ws_id, i, kind), conn_store)
        if status != 200 and status != 404:
            raise SystemExit(f"frame {i} -> HTTP {status} (всего {total})")
        if status == 404:
            failed += 1
        elif len(body) == 0:
            raise SystemExit(f"frame {i}: пустое тело")
    dt = time.perf_counter() - t0
    return dt, failed


def restart_container():
    subprocess.run(
        ["docker", "restart", CONTAINER], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # Ждём готовности HTTP.
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("localhost", 8001, timeout=5)
            conn.request("GET", "/api/v1/workspaces/")
            res = conn.getresponse()
            res.read()
            conn.close()
            if res.status == 200:
                return
        except OSError:
            pass
        time.sleep(1.0)
    raise SystemExit("Контейнер не поднялся за 180 c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-restart", action="store_true",
                    help="не перезапускать контейнер перед каждым видео")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("targets", nargs="*", default=None,
                    help="workspace@kind (original|visualization); по умолчанию все")
    args = ap.parse_args()

    ws_list = list_workspaces(args.url)

    targets = []
    if args.targets:
        for raw in args.targets:
            if "@" in raw:
                ws_id, kind = raw.split("@", 1)
            else:
                ws_id = raw
                kind = None
            targets.append((ws_id, kind))
    else:
        for w in ws_list:
            targets.append((w["id"], None))

    print("Видео в бенчмарке:")
    rows = []
    for ws_id, kind in targets:
        info = next((w for w in ws_list if w["id"] == ws_id), None)
        if info is None:
            print(f"  ВНИМАНИЕ: workspace '{ws_id}' не найден — пропускаю")
            continue
        kinds = [kind] if kind else (
            ["original", "visualization"] if info["source_name"] != info["preview_name"]
            else ["visualization"]
        )
        fp = sorted({f for f in (info["source_name"], info["preview_name"]) if f})
        name = fp[0] if len(fp) == 1 else "+".join(fp)
        for k in kinds:
            rows.append((ws_id, k, info["total_frames"]))
            print(f"  {ws_id:<60} {k:<14} {name}")

    print("\nМетрика = среднее{T_fwd, T_bwd} / (W*H*N),  сек/пиксель\n")
    header = (f"{'workspace':<60} {'kind':<14} {'W':>5} {'H':>5} {'N':>5} "
              f"{'404':>4} {'T_fwd':>9} {'T_bwd':>9} {'метрика':>12} {'кадр/с':>8}")
    print(header)
    print("-" * len(header))

    for ws_id, kind, total in rows:
        meta_status, meta_body = http_get_json(
            args.url, f"/api/v1/workspaces/{quote(ws_id)}/", {}
        )
        meta = json.loads(meta_body)
        w, h = meta["width"], meta["height"]
        n = meta["total_frames"]

        if not args.no_restart:
            restart_container()

        t_fwd, f404_fwd = one_direction(args.url, ws_id, kind, range(n), n)
        t_bwd, f404_bwd = one_direction(args.url, ws_id, kind, range(n - 1, -1, -1), n)

        pixels = w * h * n
        metric = (t_fwd + t_bwd) / 2 / pixels
        fps = n / ((t_fwd + t_bwd) / 2)
        print(f"{ws_id:<60} {kind:<14} {w:>5} {h:>5} {n:>5} "
              f"{f404_fwd + f404_bwd:>4} {t_fwd:>8.2f}s {t_bwd:>8.2f}s "
              f"{metric:>10.9f} {fps:>7.1f}")

    print("\nГотово.")


if __name__ == "__main__":
    main()