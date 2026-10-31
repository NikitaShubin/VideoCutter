# -*- coding: utf-8 -*-
"""ExportLedger — единственный модуль состояния экспорта.

Владеет in-process реестром, очередью, флагами отмены, sidecar на диске,
хешем свежести, проверкой наличия файлов и построением URL. Не импортирует
Django и workspace: вызывающий передаёт workspace-подобный объект
(``name``, ``path``, ``original``, ``load_fragments()``).

Состояние задачи живёт на диске; память — только кэш. Готовый экспорт
сверяется с текущими границами фрагментов и идентичностью видео, поэтому
замена файла или разметки мимо API не оставляет ложный «done».
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Iterable, List, Optional, Sequence

#: Sidecar готового экспорта рядом с файлами (переживает рестарт процесса).
SIDECAR = ".export-state.json"


class ExportLedger:
    """Реестр состояния экспорта с узким interface.

    Публичное: ``status``, ``current_done``, ``submit``, ``promote``,
    ``finish``, ``fail``, ``cancel``, ``request_cancel``, ``rename``,
    ``forget``. Внутри — четыре прежних хранилища, но вызывающий их не знает.
    """

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.cancel_set: set[str] = set()
        self.queue: List[str] = []
        self.sidecar_name = SIDECAR

    # ─── идентичность и свежесть ──────────────────────────────────────────

    def video_identity(self, path: str) -> Optional[dict]:
        """Идентичность видео {size, mtime_ns} (None — файла нет)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def task_hash(self, frags: Iterable[dict], size: int, mtime_ns: int) -> str:
        """Хеш состояния нарезки: границы (порядок не важен) + видео."""
        canon = json.dumps({
            "fragments": sorted([f["start"], f["end"]] for f in frags),
            "video": {"size": size, "mtime_ns": mtime_ns},
        }, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    # ─── файлы и sidecar ──────────────────────────────────────────────────

    def _out_dir(self, ws) -> str:
        return os.path.join(ws.path, "exports")

    def outputs_present(self, out_dir: str, files) -> bool:
        """Все файлы записи на месте и ненулевые (пустой список — да)."""
        for item in files or []:
            name = item.get("filename", "") if isinstance(item, dict) else item
            full = os.path.join(out_dir, os.path.basename(name or ""))
            try:
                if not os.path.isfile(full) or os.path.getsize(full) == 0:
                    return False
            except OSError:
                return False
        return True

    def read_sidecar(self, out_dir: str) -> Optional[dict]:
        """Читает sidecar; битый/отсутствующий — None (не роняем задачу)."""
        try:
            with open(os.path.join(out_dir, self.sidecar_name),
                      encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write_sidecar(self, out_dir: str, task_hash, created: Sequence[str]) -> None:
        try:
            with open(os.path.join(out_dir, self.sidecar_name),
                      "w", encoding="utf-8") as f:
                json.dump({
                    "hash": task_hash,
                    "files": [os.path.basename(p) for p in created],
                }, f)
        except OSError:
            pass

    def drop_outputs(self, ws, *, remove=os.remove) -> None:
        """Удаляет прежние наши нарезки (sidecar + именной шаблон)."""
        out_dir = self._out_dir(ws)
        victims = set()
        saved = self.read_sidecar(out_dir)
        if saved:
            for filename in saved.get("files") or []:
                victims.add(os.path.basename(filename))
        try:
            for filename in os.listdir(out_dir):
                if "_fragment_" in filename:
                    victims.add(filename)
        except OSError:
            return
        for filename in victims:
            try:
                remove(os.path.join(out_dir, filename))
            except OSError:
                pass

    # ─── состояние для списка и статуса ──────────────────────────────────

    def snapshot(self, ws_id: str) -> Optional[dict]:
        """Копия сырого состояния (для HTTP-статуса)."""
        with self.lock:
            state = self.states.get(ws_id)
            return dict(state) if state is not None else None

    def status(self, ws) -> Optional[dict]:
        """Бейдж списка задач (None — тихо).

        ``done`` из памяти сверяется с диском: нарезки, снесённые вручную
        мимо API, иначе светились бы «готовыми» до рестарта. Протухшая
        запись выкидывается из памяти.
        """
        with self.lock:
            exp = self.states.get(ws.name)
        if exp is None:
            return None
        badge = {
            "state": exp.get("state"),
            "index": exp.get("index", 0),
            "total": exp.get("total", 1),
            "error": exp.get("error", ""),
        }
        if badge["state"] != "done":
            return badge
        want = self._current_want(ws)
        if want is None or self._done_files(ws, want, require_hash=False) is None:
            self._forget_state(ws.name)
            return None
        return badge

    def _current_want(self, ws, video_identity=None, task_hash=None):
        """Хеш текущих границ+видео (None — предмета экспорта нет)."""
        frags = ws.load_fragments()
        if not frags or not ws.original:
            return None
        vid = (video_identity or self.video_identity)(ws.original)
        if vid is None:
            return None
        return (task_hash or self.task_hash)(
            frags, vid["size"], vid["mtime_ns"])

    def _done_files(self, ws, want: str, *, require_hash: bool = True):
        """Файлы актуального done из памяти или sidecar (None — не готово).

        Память — только кэш: запись с несуществующими файлами выкидывается,
        дальше истина на диске (sidecar мог обновить другой процесс).
        ``require_hash=False`` — для бейджа: служебные done без хеша честны
        по наличию файлов (так исторически показывает список).
        """
        with self.lock:
            state = self.states.get(ws.name)
        if state and state.get("state") == "done":
            same = (state.get("hash") == want if require_hash
                    else ("hash" not in state or state["hash"] == want))
            if same:
                if self.outputs_present(self._out_dir(ws), state.get("files")):
                    return state.get("files", [])
                self._forget_state(ws.name)
        disk = self.status_from_disk(ws, want)
        return disk["files"] if disk is not None else None

    def _forget_state(self, ws_id: str) -> None:
        with self.lock:
            self.states.pop(ws_id, None)

    def current_done(self, ws, *, video_identity=None, task_hash=None):
        """Готовый результат под ТЕКУЩЕЕ состояние (None — гнать заново).

        ``video_identity``/``task_hash`` можно подменить (тесты, диагностика),
        не трогая реализацию модуля.
        """
        want = self._current_want(ws, video_identity, task_hash)
        if want is None:
            return None
        files = self._done_files(ws, want)
        if files is None:
            return None
        return {"state": "done", "files": files}

    def status_from_disk(self, ws, want: str) -> Optional[dict]:
        """Статус done из sidecar (после рестарта) или None."""
        out_dir = self._out_dir(ws)
        saved = self.read_sidecar(out_dir)
        if saved is None:
            return None
        if not saved.get("hash") or saved["hash"] != want:
            return None
        urls = []
        for i, filename in enumerate(saved.get("files") or [], 1):
            full = os.path.join(out_dir, os.path.basename(filename))
            try:
                # Нулевой размер — недописанный файл убитого экспорта.
                if not os.path.isfile(full) or os.path.getsize(full) == 0:
                    return None
            except OSError:
                return None
            urls.append({
                "index": i,
                "filename": os.path.basename(filename),
                "url": f"/api/v1/pairs/{ws.name}/export/{os.path.basename(filename)}",
            })
        if not urls:
            return None
        return {"state": "done", "files": urls}

    # ─── очередь и слоты ──────────────────────────────────────────────────

    def _requeue_positions(self) -> None:
        """Пересчитать position очереди (вызывать под lock)."""
        for pos, ws_id in enumerate(self.queue, 1):
            st = self.states.get(ws_id)
            if st is not None and st.get("state") == "queued":
                st["position"] = pos

    def _running_count(self) -> int:
        """Число бегущих экспортов (вызывать под lock)."""
        return sum(1 for s in self.states.values()
                   if s.get("state") == "running")

    def running_count(self) -> int:
        with self.lock:
            return self._running_count()

    def is_running(self, ws_id: str) -> bool:
        with self.lock:
            return self.states.get(ws_id, {}).get("state") == "running"

    def enqueue(self, ws_id: str, total: int) -> dict:
        """Ставит заявку в FIFO; возвращает копию состояния."""
        with self.lock:
            if ws_id not in self.queue:
                self.queue.append(ws_id)
            self.states[ws_id] = {"state": "queued", "total": total}
            self._requeue_positions()
            return dict(self.states[ws_id])

    def submit(self, ws_id: str, total: int, capacity: int) -> dict:
        """Стартует сразу при свободном слоте, иначе ставит в очередь."""
        with self.lock:
            if self._running_count() < capacity:
                self.states[ws_id] = {
                    "state": "running", "index": 0, "total": total,
                }
                return dict(self.states[ws_id])
            if ws_id not in self.queue:
                self.queue.append(ws_id)
            self.states[ws_id] = {"state": "queued", "total": total}
            self._requeue_positions()
            return dict(self.states[ws_id])

    def promote(self, capacity: int) -> List[str]:
        """Занять освободившиеся слоты первыми из очереди."""
        to_start = []
        with self.lock:
            running = self._running_count()
            while running < capacity and self.queue:
                ws_id = self.queue.pop(0)
                prev = self.states.get(ws_id) or {}
                self.states[ws_id] = {
                    "state": "running", "index": 0,
                    "total": prev.get("total", 1),
                }
                to_start.append(ws_id)
                running += 1
            self._requeue_positions()
        return to_start

    # ─── переходы ─────────────────────────────────────────────────────────

    def progress(self, ws_id: str, index: int, total: int) -> bool:
        """Отмечает прогресс; False — пришла отмена (проверка и запись под lock)."""
        with self.lock:
            if ws_id in self.cancel_set:
                return False
            self.states[ws_id] = {
                "state": "running", "index": index, "total": total,
            }
            return True

    def request_cancel(self, ws_id: str) -> None:
        with self.lock:
            self.cancel_set.add(ws_id)

    def cancelled(self, ws_id: str) -> bool:
        with self.lock:
            return ws_id in self.cancel_set

    def clear_cancel(self, ws_id: str) -> None:
        with self.lock:
            self.cancel_set.discard(ws_id)

    def finish(self, ws_id: str, created: Sequence[str], task_hash) -> None:
        """Фиксирует готовый экспорт и пишет sidecar."""
        urls = []
        for i, path in enumerate(created, 1):
            filename = os.path.basename(path)
            urls.append({
                "index": i,
                "filename": filename,
                "url": f"/api/v1/pairs/{ws_id}/export/{filename}",
            })
        with self.lock:
            self.states[ws_id] = {
                "state": "done", "files": urls, "hash": task_hash,
            }
        if created:
            self._write_sidecar(os.path.dirname(created[0]), task_hash, created)

    def fail(self, ws_id: str, message: str) -> None:
        with self.lock:
            self.states[ws_id] = {"state": "error", "error": message}

    def cancel(self, ws_id: str, created_now: Sequence[str]) -> None:
        """Отмена: чистит частичные файлы, снимает queued-заявку."""
        for path in created_now:
            try:
                os.remove(path)
            except OSError:
                pass
        with self.lock:
            if ws_id in self.queue:
                self.queue.remove(ws_id)
                self._requeue_positions()
            self.states[ws_id] = {"state": "cancelled"}
            self.cancel_set.discard(ws_id)

    def rename(self, old_id: str, new_id: str) -> None:
        with self.lock:
            if old_id in self.states:
                self.states[new_id] = self.states.pop(old_id)
            if old_id in self.queue:
                self.queue[self.queue.index(old_id)] = new_id
                self._requeue_positions()

    def forget(self, ws_id: str, *, keep_cancel: bool = False) -> None:
        """Забывает задачу: состояние и очередь.

        ``keep_cancel=True`` — для удаления workspace: бегущий поток ещё
        должен увидеть флаг отмены (снимет его сам в finally).
        """
        with self.lock:
            self.states.pop(ws_id, None)
            if not keep_cancel:
                self.cancel_set.discard(ws_id)
            if ws_id in self.queue:
                self.queue.remove(ws_id)
                self._requeue_positions()

    # ─── тестовая изоляция ────────────────────────────────────────────────

    def _reset_for_tests(self) -> None:
        """Сброс process-global состояния между тестами (не для прода)."""
        with self.lock:
            self.states.clear()
            self.queue.clear()
            self.cancel_set.clear()


#: Единственный экземпляр на процесс: состояние экспорта — process-global.
LEDGER = ExportLedger()
