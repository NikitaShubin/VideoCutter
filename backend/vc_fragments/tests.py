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

from django.conf import settings
from django.test import SimpleTestCase

from workspace import WORKSPACE_ROOT, _workspaces
from vc_fragments.views import EXPORTS, EXPORTS_LOCK

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
        resp = self.client.get(self.ws_list_url)
        item = next(w for w in resp.json() if w["id"] == self.ws_id)
        self.assertEqual(len(item["fragments"]), 2)
        self.assertEqual(item["position"], 0)
        self.assertTrue(isinstance(item["updated_at"], (int, float)))
        self.assertGreater(item["updated_at"], 0)

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