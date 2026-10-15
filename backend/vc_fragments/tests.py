# -*- coding: utf-8 -*-
"""Тесты workspace-based API: workspace list, fragments, export status.

Прогон:  cd backend && python manage.py test

Проект stateless: без БД (SimpleTestCase), фрагменты — fragments.tsv рядом
с видео. Внимание: Django test client c format="json" и list-payload ломается
(шлёт repr списка с application/octet-stream), поэтому PUT делаем явным JSON.
"""

import json
import os
import shutil
import tempfile
import threading
import time
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from workspace import WORKSPACE_ROOT, _workspaces
from vc_fragments.views import EXPORTS, EXPORTS_LOCK, EXPORT_CANCEL, \
    _EXPORT_QUEUE

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


class WorkspaceApiTestBase(SimpleTestCase):
    """Создаёт временный workspace с test video и fragments.tsv.

    SimpleTestCase — без БД (проект stateless).
    """

    def setUp(self):
        self._orig_root = WORKSPACE_ROOT
        self._tmpdir = tempfile.mkdtemp()
        settings.VC_WORKSPACE_ROOT = self._tmpdir

        # Пересоздаём глобальный реестр.
        _workspaces.clear()
        import workspace
        workspace.WORKSPACE_ROOT = self._tmpdir

        # Создаём workspace: директория + видео.
        self.ws_id = "test-ws"
        self.ws_dir = os.path.join(self._tmpdir, self.ws_id)
        os.makedirs(self.ws_dir, exist_ok=True)
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        self.video = os.path.join(self.ws_dir, "visualization.mp4")
        shutil.copy2(src, self.video)

        # Создаём fragments.tsv (заголовок обязателен — так пишет save_fragments).
        # Тестовое видео: 10 кадров (testsrc2, 10 fps, 1 s).
        tsv = os.path.join(self.ws_dir, "fragments.tsv")
        with open(tsv, "w") as f:
            f.write("start\tend\tcomment\n")
            f.write("0\t3\tстарт\n")
            f.write("5\t9\tфиниш\n")

        # Сброс глобального состояния экспорта между тестами.
        with EXPORTS_LOCK:
            EXPORTS.clear()
            EXPORT_CANCEL.clear()
            del _EXPORT_QUEUE[:]
        # Сброс ошибок фоновой валидации (память процесса).
        import workspace as _ws_mod

        _ws_mod.clear_all_workspace_errors()

        # Списки URL.
        self.ws_list_url = "/api/v1/workspaces/"
        self.ws_detail_url = f"/api/v1/workspaces/{self.ws_id}/"
        self.ws_meta_url = f"/api/v1/workspaces/{self.ws_id}/meta"
        self.ws_frame_url = f"/api/v1/workspaces/{self.ws_id}/frame/0/"
        self.frags_url = f"/api/v1/pairs/{self.ws_id}/fragments/"
        self.position_url = f"/api/v1/pairs/{self.ws_id}/position"
        self.export_url = f"/api/v1/pairs/{self.ws_id}/export"
        self.export_status_url = f"/api/v1/pairs/{self.ws_id}/export/status"

    def json_put(self, url, payload):
        """PUT с корректным JSON-телом (для list Django-client ломается)."""
        return self.client.put(
            url,
            data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

    def tearDown(self):
        import workspace
        # Дожидаемся фона экспорта: иначе поток переживает rmtree и пишет
        # чужой статус / читает удалённый каталог (флейк между тестами).
        deadline = time.time() + 30
        while True:
            with EXPORTS_LOCK:
                st = (EXPORTS.get(self.ws_id) or {}).get("state")
            if st not in ("running", "cancelling"):
                break
            if time.time() > deadline:
                break
            time.sleep(0.05)
        settings.VC_WORKSPACE_ROOT = self._orig_root
        workspace.WORKSPACE_ROOT = self._orig_root
        _workspaces.clear()
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class WorkspaceListTests(WorkspaceApiTestBase):
    def test_list_shows_workspace(self):
        resp = self.client.get(self.ws_list_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        ids = [w["id"] for w in resp.json()]
        self.assertIn(self.ws_id, ids)

    def test_list_item_has_preview_data(self):
        """Элемент списка несёт фрагменты/позицию/время правки — для превью."""
        import task_meta

        task_meta.save(self.ws_dir, task_meta.init_new())
        deadline = time.time() + 15
        while True:
            resp = self.client.get(self.ws_list_url)
            item = next(w for w in resp.json() if w["id"] == self.ws_id)
            if not item["indexing"]:
                break
            if time.time() > deadline:
                self.fail("background index never finished")
            time.sleep(0.05)
        self.assertEqual(len(item["fragments"]), 2)
        self.assertEqual(item["position"], 0)
        self.assertTrue(isinstance(item["updated_at"], (int, float)))
        self.assertGreater(item["updated_at"], 0)
        # Даты и бейдж экспорта — всегда в записи.
        self.assertTrue(item["created_at"])
        self.assertIsNone(item["last_opened_at"])
        self.assertFalse(item["indexing"])
        self.assertFalse(item["broken"])
        self.assertIsNone(item["export"])

    def test_list_sorted_by_updated_at_desc(self):
        """Свежие правки (mtime fragments.tsv) — вверху списка."""
        # Второй workspace без правок: старый mtime папки.
        other = os.path.join(self._tmpdir, "old-ws")
        os.makedirs(other, exist_ok=True)
        shutil.copy2(self.video, os.path.join(other, "video.mp4"))
        old = 1_000_000_000
        os.utime(other, (old, old))

        resp = self.client.get(self.ws_list_url)
        items = resp.json()
        ids = [w["id"] for w in items]
        self.assertEqual(ids[0], self.ws_id)  # у него есть свежий fragments.tsv

        # Обновляем mtime чужого TSV — он становится первым.
        tsv = os.path.join(other, "fragments.tsv")
        with open(tsv, "w") as f:
            f.write("start\tend\tcomment\n0\t3\tx\n")
        now = 2_000_000_000
        os.utime(tsv, (now, now))
        ids2 = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertEqual(ids2[0], "old-ws")
        self.assertEqual(ids2[1], self.ws_id)

    def test_detail_shows_fragments(self):
        resp = self.client.get(self.ws_detail_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["id"], self.ws_id)
        frags = body["fragments"]
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0]["comment"], "старт")
        self.assertEqual(frags[1]["start"], 5)

    def test_detail_404_for_missing(self):
        resp = self.client.get("/api/v1/workspaces/nonexistent/")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_detail_stamps_opened(self):
        """GET detail штампует открытие; без паспорта created_at пуст."""
        import task_meta

        body = self.client.get(self.ws_detail_url).json()
        self.assertTrue(body["last_opened_at"])
        meta = task_meta.load(self.ws_dir)
        self.assertIsNone(meta["created_at"])
        self.assertEqual(meta["last_opened_at"], body["last_opened_at"])
        body2 = self.client.get(self.ws_detail_url).json()
        self.assertGreaterEqual(
            body2["last_opened_at"], body["last_opened_at"])

    def test_list_shows_export_badge(self):
        """Бегущий экспорт виден в записи списка."""
        from vc_fragments.views import EXPORTS, EXPORTS_LOCK

        with EXPORTS_LOCK:
            EXPORTS[self.ws_id] = {"state": "running", "index": 2, "total": 5}
        try:
            resp = self.client.get(self.ws_list_url)
            item = next(w for w in resp.json() if w["id"] == self.ws_id)
            self.assertEqual(item["export"]["state"], "running")
            self.assertEqual(item["export"]["index"], 2)
            self.assertEqual(item["export"]["total"], 5)
        finally:
            with EXPORTS_LOCK:
                EXPORTS.pop(self.ws_id, None)

    def test_meta(self):
        resp = self.client.get(self.ws_meta_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["id"], self.ws_id)
        self.assertIn("total_frames", body)
        self.assertIn("width", body)


class FragmentsApiTests(WorkspaceApiTestBase):
    def test_get_fragments(self):
        resp = self.client.get(self.frags_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        frags = resp.json()
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0]["comment"], "старт")

    def test_put_replace_fragments(self):
        payload = [
            {"start": 1, "end": 3, "comment": "новый"},
            {"start": 5, "end": 7, "comment": ""},
        ]
        resp = self.json_put(self.frags_url, payload)
        self.assertEqual(resp.status_code, HTTP_OK)
        frags = resp.json()
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0]["start"], 1)

        # Проверяем, что TSV обновился.
        resp2 = self.client.get(self.frags_url)
        self.assertEqual(len(resp2.json()), 2)
        self.assertEqual(resp2.json()[0]["comment"], "новый")

    def test_put_preserves_comments_without_key(self):
        """Обратная совместимость: если comment не передан, старый сохраняется."""
        payload = [{"start": 0, "end": 5}]  # без comment
        resp = self.json_put(self.frags_url, payload)
        self.assertEqual(resp.status_code, HTTP_OK)
        # Старые границы (0-3, 5-9) не совпадают с (0-5) — комментарий опускается.
        self.assertEqual(resp.json()[0]["comment"], "")

    def test_put_rejects_overlap(self):
        payload = [
            {"start": 1, "end": 4},
            {"start": 3, "end": 6},
        ]
        resp = self.json_put(self.frags_url, payload)
        self.assertEqual(resp.status_code, HTTP_CONFLICT)

    def test_put_rejects_non_list(self):
        resp = self.json_put(self.frags_url, {"start": 0, "end": 5})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_rejects_missing_workspace(self):
        resp = self.json_put("/api/v1/pairs/nonexistent/fragments/", [{"start": 0, "end": 5}])
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)


class PositionApiTests(WorkspaceApiTestBase):
    def test_detail_position_default_zero(self):
        """Без строки `# position` позиция = 0 (обратная совместимость)."""
        body = self.client.get(self.ws_detail_url).json()
        self.assertEqual(body["position"], 0)

    def test_position_roundtrip_creates_metadata_line(self):
        resp = self.json_put(self.position_url, {"position": 7})
        self.assertEqual(resp.status_code, HTTP_OK)
        self.assertEqual(resp.json()["position"], 7)

        # GET и detail отражают сохранённое значение.
        self.assertEqual(self.client.get(self.position_url).json()["position"], 7)
        self.assertEqual(self.client.get(self.ws_detail_url).json()["position"], 7)

        # Позиция лежит в fragments.tsv строкой-комментарием, фрагменты целы.
        with open(os.path.join(self.ws_dir, "fragments.tsv")) as f:
            text = f.read()
        self.assertIn("# position\t7", text)
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)

    def test_put_fragments_with_position(self):
        payload = {
            "fragments": [{"start": 1, "end": 3, "comment": "новый"}],
            "position": 4,
        }
        resp = self.json_put(self.frags_url, payload)
        self.assertEqual(resp.status_code, HTTP_OK)
        self.assertEqual(self.client.get(self.position_url).json()["position"], 4)
        frags = self.client.get(self.frags_url).json()
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["comment"], "новый")

    def test_put_fragments_list_preserves_position(self):
        self.json_put(self.position_url, {"position": 6})
        resp = self.json_put(self.frags_url, [{"start": 2, "end": 4}])
        self.assertEqual(resp.status_code, HTTP_OK)
        self.assertEqual(self.client.get(self.position_url).json()["position"], 6)

    def test_position_out_of_range(self):
        for bad in (10, -1):
            resp = self.json_put(self.position_url, {"position": bad})
            self.assertEqual(resp.status_code, HTTP_BAD_REQUEST, bad)

    def test_position_rejects_missing_workspace(self):
        resp = self.json_put("/api/v1/pairs/nonexistent/position", {"position": 1})
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_fragments_ignores_metadata_line(self):
        """Метаданные `# position` не попадают в список фрагментов."""
        self.json_put(self.position_url, {"position": 5})
        frags = self.client.get(self.frags_url).json()
        self.assertEqual(len(frags), 2)
        self.assertEqual(frags[0]["comment"], "старт")


class ExportApiTests(WorkspaceApiTestBase):
    def test_export_status_idle(self):
        resp = self.client.get(self.export_status_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        self.assertEqual(resp.json()["state"], "idle")

    def test_export_empty_fragments_rejected(self):
        # Очищаем fragments.tsv.
        tsv = os.path.join(self.ws_dir, "fragments.tsv")
        with open(tsv, "w") as f:
            pass
        resp = self.client.post(self.export_url)
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_export_starts(self):
        resp = self.client.post(self.export_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["state"], "running")

    def test_cancel_idle_rejected(self):
        resp = self.client.post(self.export_url + "/cancel")
        self.assertEqual(resp.status_code, HTTP_CONFLICT)

    def test_cancel_missing_workspace(self):
        resp = self.client.post("/api/v1/pairs/nonexistent/export/cancel")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_cancel_running_cleans_up(self):
        """Отмена между фрагментами: состояние cancelled, частичные файлы зачищены."""
        import vc_fragments.views as export_views

        proceed = threading.Event()

        class FakeExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                while True:
                    if progress:
                        progress(1, len(fragments), fragments[0])
                    if proceed.wait(timeout=0.02):
                        # Флаг отмены ставится раньше release: повторный
                        # progress гарантированно бросает _ExportCancelled.
                        if progress:
                            progress(1, len(fragments), fragments[0])
                        return []

        # Прежний файл в exports/ отмена трогать не должна (чистим только
        # созданное этим запуском).
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        keep = os.path.join(out_dir, "keep.txt")
        with open(keep, "w") as f:
            f.write("old")

        with mock.patch.object(export_views, "Exporter", FakeExporter):
            resp = self.client.post(self.export_url)
            self.assertEqual(resp.status_code, HTTP_OK)

            # Ждём, пока фон дойдёт до первого progress (running c index).
            deadline = time.time() + 15
            while True:
                st = self.client.get(self.export_status_url).json()
                if st.get("index", 0) >= 1:
                    break
                if time.time() > deadline:
                    self.fail("export did not reach progress")
                time.sleep(0.02)

            # Параллельный запуск запрещён.
            resp2 = self.client.post(self.export_url)
            self.assertEqual(resp2.status_code, HTTP_CONFLICT)

            cancel = self.client.post(self.export_url + "/cancel")
            self.assertEqual(cancel.status_code, HTTP_OK)
            proceed.set()

            deadline = time.time() + 15
            while True:
                st = self.client.get(self.export_status_url).json()
                if st["state"] == "cancelled":
                    break
                self.assertNotEqual(st["state"], "error", st)
                if time.time() > deadline:
                    self.fail(f"export not cancelled: {st}")
                time.sleep(0.02)

        # Повторная отмена — 409; прежний файл цел; новых файлов нет.
        again = self.client.post(self.export_url + "/cancel")
        self.assertEqual(again.status_code, HTTP_CONFLICT)
        self.assertTrue(os.path.isfile(keep))
        self.assertEqual(sorted(os.listdir(out_dir)), ["keep.txt"])

    def test_status_restored_from_disk_after_restart(self):
        """Пустой EXPORTS (рестарт) не гонит экспорт заново: done из sidecar."""
        import vc_fragments.views as export_views

        # Нейтральное имя (без маркера роли): source == preview == файл.
        shutil.copy2(self.video, os.path.join(self.ws_dir, "video.mp4"))

        from videocutter.core.exporter import Exporter as RealExporter

        class FakeExporter:
            """Делегирует настоящему (крошечный клип — быстро)."""

            def __init__(self, src, out_dir, **k):
                self.real = RealExporter(src, out_dir)

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                return self.real.extract_fragments(
                    fragments, progress=progress, cancelled=cancelled,
                    frame_ts_ranges=frame_ts_ranges)

        with mock.patch.object(export_views, "Exporter", FakeExporter):
            resp = self.client.post(self.export_url)
            self.assertEqual(resp.status_code, HTTP_OK)
            deadline = time.time() + 30
            while True:
                st = self.client.get(self.export_status_url).json()
                if st["state"] == "done":
                    break
                self.assertNotEqual(st["state"], "error", st)
                if time.time() > deadline:
                    self.fail(f"export not done: {st}")
                time.sleep(0.05)
            self.assertTrue(st["files"])

        # Симулируем рестарт: память пуста, файлы и sidecar на месте.
        with EXPORTS_LOCK:
            EXPORTS.clear()
            EXPORT_CANCEL.clear()
        st2 = self.client.get(self.export_status_url).json()
        self.assertEqual(st2["state"], "done")
        self.assertEqual(len(st2["files"]), 2)

    def _neutral_video(self):
        """Копия видео нейтральным именем (source == preview == файл)."""
        dst = os.path.join(self.ws_dir, "video.mp4")
        if not os.path.isfile(dst):
            shutil.copy2(self.video, dst)
        return dst

    def _write_sidecar(self, out_dir, frags, video_path, files, hash=None):
        import vc_fragments.views as export_views

        if hash is None:
            st = os.stat(video_path)
            hash = export_views._task_hash(frags, st.st_size, st.st_mtime_ns)
        with open(os.path.join(out_dir, ".export-state.json"), "w") as f:
            json.dump({"hash": hash, "files": files}, f)
        return hash

    def _frags(self):
        return [{"start": 0, "end": 3}, {"start": 5, "end": 9}]

    def test_post_returns_done_when_current(self):
        """POST при актуальных файлах: сразу done, поток не стартует."""
        import vc_fragments.views as export_views

        video = self._neutral_video()
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        files = []
        for i in (1, 2):
            name = f"video_fragment_{i}.mp4"
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(b"fake")
            files.append(name)
        self._write_sidecar(out_dir, self._frags(), video, files)

        resp = self.client.post(self.export_url)
        body = resp.json()
        self.assertEqual(body["state"], "done")
        self.assertEqual(len(body["files"]), 2)
        with export_views.EXPORTS_LOCK:
            self.assertNotEqual(
                export_views.EXPORTS.get(self.ws_id, {}).get("state"), "running")

    def test_post_mismatch_cleans_and_reruns(self):
        """Протухший sidecar: старые наши файлы сносятся, идёт полный прогон."""
        import vc_fragments.views as export_views

        video = self._neutral_video()
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        stale = os.path.join(out_dir, "video_fragment_9.mp4")
        with open(stale, "wb") as f:
            f.write(b"stale")
        listed = os.path.join(out_dir, "old.mp4")
        with open(listed, "wb") as f:
            f.write(b"stale")
        with open(os.path.join(out_dir, "keep.txt"), "w") as f:
            f.write("чужой")
        with open(os.path.join(out_dir, ".export-state.json"), "w") as f:
            json.dump({"hash": "0" * 64, "files": ["old.mp4"]}, f)

        from videocutter.core.exporter import Exporter as RealExporter

        class FakeExporter:
            """Делегирует настоящему (крошечный клип — быстро)."""

            def __init__(self, src, out_dir, **k):
                self.real = RealExporter(src, out_dir)

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                return self.real.extract_fragments(
                    fragments, progress=progress, cancelled=cancelled,
                    frame_ts_ranges=frame_ts_ranges)

        with mock.patch.object(export_views, "Exporter", FakeExporter):
            resp = self.client.post(self.export_url)
            self.assertEqual(resp.json()["state"], "running")
            deadline = time.time() + 30
            while True:
                st = self.client.get(self.export_status_url).json()
                if st["state"] == "done":
                    break
                self.assertNotEqual(st["state"], "error", st)
                if time.time() > deadline:
                    self.fail(f"export not done: {st}")
                time.sleep(0.05)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(listed))
        self.assertTrue(os.path.isfile(
            os.path.join(out_dir, "video_fragment_1.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(out_dir, "keep.txt")))

    def test_post_force_reruns(self):
        """?force=1 при актуальных файлах: всё равно полный прогон."""
        import vc_fragments.views as export_views

        video = self._neutral_video()
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        files = []
        for i in (1, 2):
            name = f"video_fragment_{i}.mp4"
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(b"fake")
            files.append(name)
        self._write_sidecar(out_dir, self._frags(), video, files)

        from videocutter.core.exporter import Exporter as RealExporter

        class FakeExporter:
            """Делегирует настоящему (крошечный клип — быстро)."""

            def __init__(self, *a, **k):
                self.real = RealExporter(*a, **k)

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                return self.real.extract_fragments(
                    fragments, progress=progress, cancelled=cancelled,
                    frame_ts_ranges=frame_ts_ranges)

        with mock.patch.object(export_views, "Exporter", FakeExporter):
            resp = self.client.post(self.export_url + "?force=1")
            body = resp.json()
            self.assertEqual(body["state"], "running")

    def test_cancel_aborts_mid_fragment(self):
        """Отмена срабатывает посреди фрагмента (секунды, не граница)."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import ExportCancelled

        self._neutral_video()

        class BlockingExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                while True:
                    if cancelled is not None and cancelled():
                        raise ExportCancelled("cancelled in test")
                    time.sleep(0.02)
                return []

        with mock.patch.object(export_views, "Exporter", BlockingExporter):
            resp = self.client.post(self.export_url)
            self.assertEqual(resp.json()["state"], "running")
            t0 = time.time()
            cancel = self.client.post(self.export_url + "/cancel")
            self.assertEqual(cancel.json()["state"], "cancelling")
            deadline = t0 + 15
            while True:
                st = self.client.get(self.export_status_url).json()
                if st["state"] == "cancelled":
                    break
                self.assertNotEqual(st["state"], "error", st)
                if time.time() > deadline:
                    self.fail(f"cancel not instant: {st} "
                              f"({time.time() - t0:.1f}s)")
                time.sleep(0.05)
            self.assertLess(time.time() - t0, 15)

    def test_exporter_nice_prefix(self):
        """Exporter подставляет nice-префикс только при заданном nice."""
        from videocutter.core.exporter import Exporter

        plain = Exporter("a.mp4", "/tmp").build_command(0, 3, "/tmp/x.mp4")
        self.assertEqual(plain[0], "ffmpeg")
        niced = Exporter("a.mp4", "/tmp", nice=19).build_command(0, 3, "/tmp/x.mp4")
        self.assertEqual(niced[:3], ["nice", "-n", "19"])
        self.assertEqual(niced[3:], plain)

    def test_zero_byte_file_is_not_done(self):
        """Нулевой файл в sidecar — не готовый экспорт (idle, не done)."""
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "frag_1.mp4"), "wb") as f:
            f.write(b"fake")
        open(os.path.join(out_dir, "frag_2.mp4"), "wb").close()
        with open(os.path.join(out_dir, ".export-state.json"), "w") as f:
            json.dump(
                {"sig": "[[0,3],[5,9]]", "files": ["frag_1.mp4", "frag_2.mp4"]}, f
            )
        st = self.client.get(self.export_status_url).json()
        self.assertEqual(st["state"], "idle")

    def test_verify_cut_rejects_garbage(self):
        """Мусор вместо нарезки: ffprobe провал → FFmpegError, не silent."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        bad = os.path.join(self.ws_dir, "garbage.mp4")
        with open(bad, "wb") as f:
            f.write(b"not a video")
        with self.assertRaises(FFmpegError):
            export_views._verify_cut(self.video, 0, bad, 1)

    def test_verify_cut_rejects_count_mismatch(self):
        """Чужое число кадров: несовпадение счётчика → FFmpegError."""
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        with self.assertRaises(FFmpegError):
            export_views._verify_cut(self.video, 0, self.video, 5)

    def test_verify_cut_rejects_content_mismatch(self):
        """Счётчик сошёлся, контент чужой: SSD выше порога → FFmpegError."""
        import cv2
        import numpy as np
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        other = os.path.join(self.ws_dir, "other.mp4")
        vw = cv2.VideoWriter(
            other, cv2.VideoWriter_fourcc(*"mp4v"), 10, (96, 96))
        for _ in range(10):
            vw.write(np.zeros((96, 96, 3), dtype=np.uint8))
        vw.release()
        with self.assertRaises(FFmpegError):
            export_views._verify_cut(self.video, 0, other, 10)

    def test_verify_cut_rejects_last_frame_mismatch(self):
        """Первый кадр свой, последний чужой: ловит именно сверка хвоста.

        На статике сдвиг границ даёт верные счёт и первый кадр —
        без проверки последнего он прошёл бы незамеченным.
        """
        import cv2
        import numpy as np
        import vc_fragments.views as export_views
        from videocutter.core.exporter import FFmpegError

        cap = cv2.VideoCapture(self.video)
        frames = []
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            frames.append(fr)
        cap.release()
        self.assertGreaterEqual(len(frames), 2)
        h, w = frames[0].shape[:2]
        frames[-1] = np.full((h, w, 3), 255, dtype=np.uint8)
        forged = os.path.join(self.ws_dir, "forged.mp4")
        vw = cv2.VideoWriter(
            forged, cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
        for fr in frames:
            vw.write(fr)
        vw.release()
        with self.assertRaises(FFmpegError) as ctx:
            export_views._verify_cut(self.video, 0, forged, len(frames))
        self.assertIn("последнего", str(ctx.exception))


class ExportQueueTests(WorkspaceApiTestBase):
    """Очередь экспортов: слоты, FIFO, отмена queued, 409, force."""

    def _make_ws(self, ws_id):
        ws_dir = os.path.join(self._tmpdir, ws_id)
        os.makedirs(ws_dir, exist_ok=True)
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        shutil.copy2(src, os.path.join(ws_dir, "visualization.mp4"))
        with open(os.path.join(ws_dir, "fragments.tsv"), "w") as f:
            f.write("start\tend\tcomment\n0\t3\tстарт\n")
        return ws_id

    def _urls(self, ws_id):
        base = f"/api/v1/pairs/{ws_id}/export"
        return base, base + "/status", base + "/cancel"

    def _wait_state(self, status_url, want, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.client.get(status_url).json().get("state")
            if st == want:
                return True
            time.sleep(0.05)
        return False

    def _gated_exporter(self, gate):
        class GatedExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None,
                                  cancelled=None, frame_ts_ranges=None):
                if progress:
                    progress(1, len(fragments), fragments[0])
                gate.wait(timeout=30)
                return []

        return GatedExporter

    def test_second_export_queued_behind_first(self):
        import vc_fragments.views as export_views
        ws_b = self._make_ws("test-ws-b")
        url_b, st_b, _ = self._urls(ws_b)
        gate = threading.Event()
        with mock.patch.object(export_views, "_export_capacity",
                               return_value=1), \
                mock.patch.object(export_views, "Exporter",
                                  self._gated_exporter(gate)):
            body_a = self.client.post(self.export_url).json()
            self.assertEqual(body_a["state"], "running")
            body_b = self.client.post(url_b).json()
            self.assertEqual(body_b["state"], "queued")
            self.assertEqual(body_b["position"], 1)
            st = self.client.get(st_b).json()
            self.assertEqual(st["state"], "queued")
            gate.set()
            self.assertTrue(self._wait_state(self.export_status_url, "done"))
            self.assertTrue(self._wait_state(st_b, "done"))

    def test_cancel_queued(self):
        import vc_fragments.views as export_views
        ws_b = self._make_ws("test-ws-b")
        url_b, st_b, cancel_b = self._urls(ws_b)
        gate = threading.Event()
        with mock.patch.object(export_views, "_export_capacity",
                               return_value=1), \
                mock.patch.object(export_views, "Exporter",
                                  self._gated_exporter(gate)):
            self.client.post(self.export_url)
            self.client.post(url_b)
            resp = self.client.post(cancel_b)
            self.assertEqual(resp.status_code, HTTP_OK)
            self.assertEqual(resp.json()["state"], "cancelled")
            gate.set()
            self.assertTrue(self._wait_state(self.export_status_url, "done"))
            # Снятый с очереди не стартовал.
            self.assertEqual(self.client.get(st_b).json()["state"],
                             "cancelled")

    def test_double_post_conflict(self):
        import vc_fragments.views as export_views
        gate = threading.Event()
        with mock.patch.object(export_views, "_export_capacity",
                               return_value=1), \
                mock.patch.object(export_views, "Exporter",
                                  self._gated_exporter(gate)):
            self.client.post(self.export_url)
            resp = self.client.post(self.export_url)
            self.assertEqual(resp.status_code, HTTP_CONFLICT)
            gate.set()
            self.assertTrue(self._wait_state(self.export_status_url, "done"))

    def test_force_requeues_single(self):
        import vc_fragments.views as export_views
        from vc_fragments.views import _EXPORT_QUEUE as _EQ
        ws_b = self._make_ws("test-ws-b")
        url_b, st_b, _ = self._urls(ws_b)
        gate = threading.Event()
        with mock.patch.object(export_views, "_export_capacity",
                               return_value=1), \
                mock.patch.object(export_views, "Exporter",
                                  self._gated_exporter(gate)):
            self.client.post(self.export_url)
            self.client.post(url_b)
            resp = self.client.post(url_b + "?force=1")
            self.assertEqual(resp.status_code, HTTP_OK)
            self.assertEqual(resp.json()["state"], "queued")
            self.assertEqual(len(_EQ), 1)
            gate.set()
            self.assertTrue(self._wait_state(self.export_status_url, "done"))
            self.assertTrue(self._wait_state(st_b, "done"))

    def test_positions_recompute_after_cancel(self):
        import vc_fragments.views as export_views
        ws_b = self._make_ws("test-ws-b")
        ws_c = self._make_ws("test-ws-c")
        _, _, cancel_b = self._urls(ws_b)
        _, st_c, _ = self._urls(ws_c)
        gate = threading.Event()
        with mock.patch.object(export_views, "_export_capacity",
                               return_value=1), \
                mock.patch.object(export_views, "Exporter",
                                  self._gated_exporter(gate)):
            self.client.post(self.export_url)
            self.client.post(f"/api/v1/pairs/{ws_b}/export")
            body_c = self.client.post(f"/api/v1/pairs/{ws_c}/export").json()
            self.assertEqual(body_c["position"], 2)
            self.client.post(cancel_b)
            st = self.client.get(st_c).json()
            self.assertEqual(st["position"], 1)
            gate.set()
            self.assertTrue(self._wait_state(self.export_status_url, "done"))
            self.assertTrue(self._wait_state(st_c, "done"))


class SettingsApiTests(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        self.settings_url = f"/api/v1/pairs/{self.ws_id}/settings"

    def test_get_settings_default(self):
        resp = self.client.get(self.settings_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["quality"], 78)
        self.assertEqual(body["scale"], 0.75)

    def test_put_settings_saves_and_restores(self):
        resp = self.json_put(self.settings_url, {"quality": 90, "scale": 0.5})
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["quality"], 90)
        self.assertAlmostEqual(body["scale"], 0.5, places=2)

        # Проверяем через GET и detail — настройки восстанавливаются.
        self.assertEqual(self.client.get(self.settings_url).json()["quality"], 90)
        detail = self.client.get(self.ws_detail_url).json()
        self.assertEqual(detail["quality"], 90)
        self.assertAlmostEqual(detail["scale"], 0.5, places=2)

    def test_put_settings_persists_in_tsv(self):
        self.json_put(self.settings_url, {"quality": 50, "scale": 0.25})
        with open(os.path.join(self.ws_dir, "fragments.tsv")) as f:
            text = f.read()
        self.assertIn("# settings\t50\t0.25", text)
        # Фрагменты не пострадали.
        frags = self.client.get(self.frags_url).json()
        self.assertEqual(len(frags), 2)

    def test_put_settings_out_of_range(self):
        for bad in [{"quality": 5}, {"quality": 100}, {"scale": 0.01}, {"scale": 1.5}]:
            resp = self.json_put(self.settings_url, bad)
            self.assertEqual(resp.status_code, HTTP_BAD_REQUEST, bad)

    def test_settings_rejects_missing_workspace(self):
        resp = self.client.get("/api/v1/pairs/nonexistent/settings")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_settings_405_for_post(self):
        resp = self.client.post(self.settings_url)
        self.assertEqual(resp.status_code, 405)


class ExportNiceFallbackTests(WorkspaceApiTestBase):
    def test_export_nice_bad_value_fallback(self):
        import importlib
        import sys
        import vc_fragments.views as v
        tmod = sys.modules[__name__]
        orig = os.environ.get("VC_EXPORT_NICE")
        try:
            with mock.patch.dict(os.environ, {"VC_EXPORT_NICE": "not-a-number"}):
                importlib.reload(v)
            self.assertEqual(v.EXPORT_NICE, 19)
        finally:
            if orig is None:
                os.environ.pop("VC_EXPORT_NICE", None)
            else:
                os.environ["VC_EXPORT_NICE"] = orig
            importlib.reload(v)
            tmod.EXPORTS = v.EXPORTS
            tmod.EXPORTS_LOCK = v.EXPORTS_LOCK
            tmod.EXPORT_CANCEL = v.EXPORT_CANCEL
            tmod._EXPORT_QUEUE = v._EXPORT_QUEUE


class ExportCapacityEnvTests(WorkspaceApiTestBase):
    def test_capacity_valid_and_invalid(self):
        import vc_fragments.views as v
        with mock.patch.dict(os.environ, {"VC_EXPORT_WORKERS": "2"}):
            self.assertEqual(v._export_capacity(), 2)
        with mock.patch.dict(os.environ, {"VC_EXPORT_WORKERS": "0"}):
            self.assertEqual(v._export_capacity(), 1)
        with mock.patch.dict(os.environ, {"VC_EXPORT_WORKERS": "bad"}):
            self.assertGreaterEqual(v._export_capacity(), 1)


class VideoIdentityAndStateTests(WorkspaceApiTestBase):
    def test_video_identity_missing(self):
        import vc_fragments.views as v
        self.assertIsNone(v._video_identity(os.path.join(self.ws_dir, "nope.mp4")))

    def test_export_failed_direct(self):
        import vc_fragments.views as v
        v._export_failed(self.ws_id, "boom")
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error"], "boom")

    def test_export_finished_ok_sidecar_oserror(self):
        import vc_fragments.views as v
        created = [os.path.join(self.ws_dir, "exports", "a_fragment_1.mp4")]
        with mock.patch("builtins.open", side_effect=OSError("ro")):
            v._export_finished_ok(self.ws_id, created, "hash123")
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "done")

    def test_export_cancelled_cleans_partial(self):
        import vc_fragments.views as v
        keep = os.path.join(self.ws_dir, "gone.mp4")
        Path = os.path.join(self.ws_dir, "todel.mp4")
        with open(Path, "wb") as f:
            f.write(b"x")
        v._export_cancelled(self.ws_id, [Path, keep])
        self.assertFalse(os.path.exists(Path))
        with EXPORTS_LOCK:
            self.assertEqual(EXPORTS.get(self.ws_id, {}).get("state"), "cancelled")


class ProbeDecodeBranchTests(WorkspaceApiTestBase):
    def test_ffprobe_oserror(self):
        import vc_fragments.views as v
        with mock.patch.object(v.subprocess, "run", side_effect=OSError("no bin")):
            self.assertIsNone(v._ffprobe_frame_count(self.video))

    def test_ffprobe_nonzero(self):
        import vc_fragments.views as v
        m = mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(v.subprocess, "run", return_value=m):
            self.assertIsNone(v._ffprobe_frame_count(self.video))

    def test_ffprobe_bad_stdout(self):
        import vc_fragments.views as v
        m = mock.Mock(returncode=0, stdout="oops\n", stderr="")
        with mock.patch.object(v.subprocess, "run", return_value=m):
            self.assertIsNone(v._ffprobe_frame_count(self.video))
        m2 = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(v.subprocess, "run", return_value=m2):
            self.assertIsNone(v._ffprobe_frame_count(self.video))

    def test_decode_thumbs_oserror(self):
        import vc_fragments.views as v
        with mock.patch.object(v.subprocess, "run", side_effect=OSError("no bin")):
            self.assertIsNone(v._decode_thumbs(self.video, 3))

    def test_decode_thumbs_nonzero(self):
        import vc_fragments.views as v
        m = mock.Mock(returncode=1, stdout=b"", stderr=b"")
        with mock.patch.object(v.subprocess, "run", return_value=m):
            self.assertIsNone(v._decode_thumbs(self.video, 3))

    def test_decode_thumbs_misaligned(self):
        import vc_fragments.views as v
        m = mock.Mock(returncode=0, stdout=b"x" * 10)
        with mock.patch.object(v.subprocess, "run", return_value=m):
            self.assertIsNone(v._decode_thumbs(self.video, 3))


class VerifyCutExtraTests(WorkspaceApiTestBase):
    def test_verify_cut_decode_fail(self):
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError
        with mock.patch.object(v, "_ffprobe_frame_count", return_value=2), \
                mock.patch.object(v, "_decode_thumbs", return_value=None):
            with self.assertRaises(FFmpegError):
                v._verify_cut(self.video, 0, self.video, 2)

    def test_verify_cut_etalon_fail(self):
        import numpy as np
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError
        got = [np.zeros(2304, dtype=np.float32) for _ in range(2)]
        with mock.patch.object(v, "_ffprobe_frame_count", return_value=2), \
                mock.patch.object(v, "_decode_thumbs", side_effect=[got, None]):
            with self.assertRaises(FFmpegError):
                v._verify_cut(self.video, 0, self.video, 2)
        with mock.patch.object(v, "_ffprobe_frame_count", return_value=2), \
                mock.patch.object(v, "_decode_thumbs", side_effect=[got, [np.zeros(2304, dtype=np.float32)]]):
            with self.assertRaises(FFmpegError):
                v._verify_cut(self.video, 0, self.video, 2)

    def test_verify_cut_shift_detected(self):
        import numpy as np
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError
        exp0 = np.zeros(2304, dtype=np.float32)
        exp1 = np.full(2304, 255, dtype=np.float32)
        exp2 = np.zeros(2304, dtype=np.float32)
        got = [np.full(2304, 255, dtype=np.float32),
               np.zeros(2304, dtype=np.float32),
               np.zeros(2304, dtype=np.float32)]
        exp_all = [exp0, exp1, exp2]
        with mock.patch.object(v, "_ffprobe_frame_count", return_value=3), \
                mock.patch.object(v, "_decode_thumbs", side_effect=[got, exp_all]):
            with self.assertRaises(FFmpegError) as ctx:
                v._verify_cut(self.video, 0, self.video, 3)
        self.assertIn("сдвиг", str(ctx.exception))


class RunExportErrorTests(WorkspaceApiTestBase):
    def test_run_export_ws_missing(self):
        import vc_fragments.views as v
        v._run_export("ghost-ws-xyz-missing")
        with EXPORTS_LOCK:
            st = EXPORTS.get("ghost-ws-xyz-missing")
        self.assertIsNotNone(st)
        self.assertEqual(st["state"], "error")

    def test_run_export_no_frags(self):
        import vc_fragments.views as v
        ws_dir = os.path.join(self._tmpdir, "empty-ws")
        os.makedirs(ws_dir, exist_ok=True)
        shutil.copy2(self.video, os.path.join(ws_dir, "visualization.mp4"))
        with open(os.path.join(ws_dir, "fragments.tsv"), "w") as f:
            f.write("start\tend\tcomment\n")
        v._run_export("empty-ws")
        with EXPORTS_LOCK:
            st = EXPORTS.get("empty-ws")
        self.assertEqual(st["state"], "error")

    def test_run_export_fragment_out_of_range(self):
        import vc_fragments.views as v
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=[0, 1]), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=False):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")
        self.assertIn("вне диапазона", st["error"])

    def test_run_export_by_count_out_of_range(self):
        import vc_fragments.views as v
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=[0, 1]), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=True):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")
        self.assertIn("вне диапазона", st["error"])

    def test_run_export_by_count_valid(self):
        import vc_fragments.views as v

        class EmptyExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                self.seen_ranges = frame_ts_ranges
                return []

        exp = EmptyExporter
        vpts = [i * 100000 for i in range(12)]
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=vpts), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=True), \
                mock.patch.object(v, "Exporter", exp):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "done")

    def test_run_export_cancel_listdir_oserror(self):
        import vc_fragments.views as v
        real_listdir = os.listdir
        exp_calls = []

        def fake_listdir(p, *a, **k):
            if os.path.abspath(p) == os.path.abspath(os.path.join(self.ws_dir, "exports")):
                exp_calls.append(p)
                if len(exp_calls) == 1:
                    return []
                raise OSError("gone")
            return real_listdir(p, *a, **k)

        class CancelExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                raise v._ExportCancelled()

        with mock.patch.object(v, "Exporter", CancelExporter), \
                mock.patch.object(v.os, "listdir", side_effect=fake_listdir):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "cancelled")

    def test_run_export_ffmpeg_error_cleanup(self):
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError

        class FailExporter:
            def __init__(self, src, out_dir, **k):
                self.out_dir = out_dir

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                with open(os.path.join(self.out_dir, "video_fragment_1.mp4"), "wb") as f:
                    f.write(b"x")
                raise FFmpegError("boom-ffmpeg")

        vpts = [i * 100000 for i in range(12)]
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=vpts), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=False), \
                mock.patch.object(v, "Exporter", FailExporter):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")
        self.assertIn("boom-ffmpeg", st["error"])
        out_dir = os.path.join(self.ws_dir, "exports")
        if os.path.isdir(out_dir):
            self.assertNotIn("video_fragment_1.mp4", os.listdir(out_dir))

    def test_run_export_ffmpeg_error_listdir_and_remove_oserror(self):
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError

        class FailExporter2:
            def __init__(self, src, out_dir, **k):
                self.out_dir = out_dir

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                with open(os.path.join(self.out_dir, "video_fragment_9.mp4"), "wb") as f:
                    f.write(b"x")
                raise FFmpegError("boom2")

        vpts = [i * 100000 for i in range(12)]
        real_listdir = os.listdir
        out_dir = os.path.join(self.ws_dir, "exports")
        calls = []

        def fake_listdir(p, *a, **k):
            if os.path.abspath(p) == os.path.abspath(out_dir):
                calls.append(p)
                if len(calls) == 1:
                    return real_listdir(p, *a, **k)
                raise OSError("gone")
            return real_listdir(p, *a, **k)

        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=vpts), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=False), \
                mock.patch.object(v, "Exporter", FailExporter2), \
                mock.patch.object(v.os, "listdir", side_effect=fake_listdir), \
                mock.patch.object(v.os, "remove", side_effect=OSError("ro")):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")

    def test_run_export_ffmpeg_remove_oserror(self):
        import vc_fragments.views as v
        from videocutter.core.exporter import FFmpegError

        class FailExporter3:
            def __init__(self, src, out_dir, **k):
                self.out_dir = out_dir

            def extract_fragments(self, fragments, progress=None, cancelled=None,
                                  frame_ts_ranges=None):
                with open(os.path.join(self.out_dir, "video_fragment_7.mp4"), "wb") as f:
                    f.write(b"x")
                raise FFmpegError("boom3")

        vpts = [i * 100000 for i in range(12)]
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=vpts), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=False), \
                mock.patch.object(v, "Exporter", FailExporter3), \
                mock.patch.object(v.os, "remove", side_effect=OSError("ro")):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")

    def test_run_export_generic_exception(self):
        import vc_fragments.views as v

        class BoomExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, *a, **k):
                raise RuntimeError("boom-generic")

        vpts = [i * 100000 for i in range(12)]
        with mock.patch.object(v.frame_provider, "get_visible_pts", return_value=vpts), \
                mock.patch.object(v.frame_provider, "index_pts_unreliable", return_value=False), \
                mock.patch.object(v, "Exporter", BoomExporter):
            v._run_export(self.ws_id)
        with EXPORTS_LOCK:
            st = EXPORTS.get(self.ws_id)
        self.assertEqual(st["state"], "error")
        self.assertIn("boom-generic", st["error"])


class FragmentsValidationExtraTests(WorkspaceApiTestBase):
    def test_put_bad_json(self):
        resp = self.client.put(self.frags_url, data="not json{",
                               content_type="application/json")
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_bad_position_type(self):
        resp = self.json_put(self.frags_url, {"fragments": [{"start": 0, "end": 1}],
                                              "position": "bad"})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_position_out_of_range(self):
        resp = self.json_put(self.frags_url, {"fragments": [{"start": 0, "end": 1}],
                                              "position": 9999})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_item_not_dict(self):
        resp = self.json_put(self.frags_url, [123])
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_item_bad_start_end(self):
        resp = self.json_put(self.frags_url, [{"start": "x", "end": 1}])
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)
        resp2 = self.json_put(self.frags_url, [{"start": 0}])
        self.assertEqual(resp2.status_code, HTTP_BAD_REQUEST)

    def test_put_item_out_of_range(self):
        resp = self.json_put(self.frags_url, [{"start": -1, "end": 1}])
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)
        resp2 = self.json_put(self.frags_url, [{"start": 0, "end": 9999}])
        self.assertEqual(resp2.status_code, HTTP_BAD_REQUEST)
        resp3 = self.json_put(self.frags_url, [{"start": 5, "end": 2}])
        self.assertEqual(resp3.status_code, HTTP_BAD_REQUEST)


class PositionValidationExtraTests(WorkspaceApiTestBase):
    def test_put_bad_json(self):
        resp = self.client.put(self.position_url, data="not json{",
                               content_type="application/json")
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_missing_position(self):
        resp = self.json_put(self.position_url, {})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)
        resp2 = self.json_put(self.position_url, [1, 2])
        self.assertEqual(resp2.status_code, HTTP_BAD_REQUEST)

    def test_put_bad_position_type(self):
        resp = self.json_put(self.position_url, {"position": "bad"})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)


class SettingsValidationExtraTests(WorkspaceApiTestBase):
    def setUp(self):
        super().setUp()
        self.settings_url = f"/api/v1/pairs/{self.ws_id}/settings"

    def test_put_bad_json(self):
        resp = self.client.put(self.settings_url, data="not json{",
                               content_type="application/json")
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_not_dict(self):
        resp = self.json_put(self.settings_url, [1, 2])
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)

    def test_put_bad_quality_scale_type(self):
        resp = self.json_put(self.settings_url, {"quality": "bad"})
        self.assertEqual(resp.status_code, HTTP_BAD_REQUEST)
        resp2 = self.json_put(self.settings_url, {"scale": "bad"})
        self.assertEqual(resp2.status_code, HTTP_BAD_REQUEST)


class ExportHttpExtraTests(WorkspaceApiTestBase):
    def test_export_missing_workspace(self):
        resp = self.client.post("/api/v1/pairs/nonexistent/export")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_force_over_running(self):
        import vc_fragments.views as export_views
        gate = threading.Event()

        class GatedExporter:
            def __init__(self, *a, **k):
                pass

            def extract_fragments(self, fragments, progress=None,
                                  cancelled=None, frame_ts_ranges=None):
                from videocutter.core.exporter import ExportCancelled
                if progress:
                    progress(1, len(fragments), fragments[0])
                while not gate.is_set():
                    if cancelled is not None and cancelled():
                        raise ExportCancelled("cancelled")
                    time.sleep(0.02)
                return []

        with mock.patch.object(export_views, "_export_capacity", return_value=1), \
                mock.patch.object(export_views, "Exporter", GatedExporter):
            try:
                body = self.client.post(self.export_url).json()
                self.assertEqual(body["state"], "running")
                deadline = time.time() + 15
                while True:
                    st = self.client.get(self.export_status_url).json()
                    if st.get("index", 0) >= 1:
                        break
                    if time.time() > deadline:
                        self.fail("did not reach running")
                    time.sleep(0.02)
                resp = self.client.post(self.export_url + "?force=1")
                self.assertEqual(resp.status_code, HTTP_OK)
                deadline = time.time() + 15
                while True:
                    with EXPORTS_LOCK:
                        cancelled = self.ws_id in EXPORT_CANCEL
                        q = list(_EXPORT_QUEUE)
                    if cancelled or q:
                        break
                    if time.time() > deadline:
                        break
                    time.sleep(0.02)
                gate.set()
                deadline = time.time() + 30
                while True:
                    st = self.client.get(self.export_status_url).json()
                    if st["state"] == "done":
                        break
                    if time.time() > deadline:
                        self.fail(f"force rerun not done: {st}")
                    time.sleep(0.05)
            finally:
                gate.set()

    def test_status_error(self):
        with EXPORTS_LOCK:
            EXPORTS[self.ws_id] = {"state": "error", "error": "boom"}
        resp = self.client.get(self.export_status_url)
        self.assertEqual(resp.status_code, HTTP_OK)
        body = resp.json()
        self.assertEqual(body["state"], "error")

    def test_current_done_ws_missing(self):
        import vc_fragments.views as v
        self.assertIsNone(v._current_done("ghost-ws-zzz"))
        resp = self.client.get("/api/v1/pairs/ghost-ws-zzz/export/status")
        self.assertEqual(resp.json()["state"], "idle")

    def test_current_done_no_frags_or_no_video(self):
        import vc_fragments.views as v
        with mock.patch.object(v, "get_workspace") as gw:
            m = mock.Mock()
            m.load_fragments.return_value = []
            m.original = self.video
            gw.return_value = m
            self.assertIsNone(v._current_done(self.ws_id))
            m2 = mock.Mock()
            m2.load_fragments.return_value = [{"start": 0, "end": 1}]
            m2.original = ""
            gw.return_value = m2
            self.assertIsNone(v._current_done(self.ws_id))

    def test_current_done_no_identity(self):
        import vc_fragments.views as v
        with mock.patch.object(v, "_video_identity", return_value=None):
            self.assertIsNone(v._current_done(self.ws_id))


class DiskStatusExtraTests(WorkspaceApiTestBase):
    def _write_sidecar(self, out_dir, payload):
        with open(os.path.join(out_dir, ".export-state.json"), "w") as f:
            json.dump(payload, f)

    def test_drop_previous_remove_oserror(self):
        import vc_fragments.views as v
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        victim = os.path.join(out_dir, "video_fragment_1.mp4")
        with open(victim, "wb") as f:
            f.write(b"x")
        self._write_sidecar(out_dir, {"hash": "h", "files": ["video_fragment_1.mp4"]})
        with mock.patch.object(v.os, "remove", side_effect=OSError("ro")):
            v._drop_previous_outputs(v.get_workspace(self.ws_id))
        self.assertTrue(os.path.isfile(victim))

    def test_status_from_disk_ws_missing(self):
        import vc_fragments.views as v
        self.assertIsNone(v._status_from_disk("ghost-ws-zzz", "h"))

    def test_status_from_disk_missing_file(self):
        import vc_fragments.views as v
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        self._write_sidecar(out_dir, {"hash": "abc", "files": ["missing.mp4"]})
        self.assertIsNone(v._status_from_disk(self.ws_id, "abc"))

    def test_status_from_disk_oserror(self):
        import vc_fragments.views as v
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "a.mp4"), "wb") as f:
            f.write(b"x")
        self._write_sidecar(out_dir, {"hash": "abc", "files": ["a.mp4"]})
        with mock.patch("os.path.getsize", side_effect=OSError("gone")):
            self.assertIsNone(v._status_from_disk(self.ws_id, "abc"))

    def test_status_from_disk_empty_files(self):
        import vc_fragments.views as v
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        self._write_sidecar(out_dir, {"hash": "abc", "files": []})
        self.assertIsNone(v._status_from_disk(self.ws_id, "abc"))


class DownloadTests(WorkspaceApiTestBase):
    def test_download_missing_workspace(self):
        resp = self.client.get("/api/v1/pairs/nonexistent/export/a.mp4")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_download_missing_file(self):
        resp = self.client.get(f"/api/v1/pairs/{self.ws_id}/export/nope.mp4")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_download_traversal(self):
        resp = self.client.get(f"/api/v1/pairs/{self.ws_id}/export/../fragments.tsv")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_download_ok(self):
        out_dir = os.path.join(self.ws_dir, "exports")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "hello.mp4"), "wb") as f:
            f.write(b"data123")
        resp = self.client.get(f"/api/v1/pairs/{self.ws_id}/export/hello.mp4")
        self.assertEqual(resp.status_code, HTTP_OK)
        body = b"".join(resp.streaming_content)
        resp.close()
        self.assertEqual(body, b"data123")
