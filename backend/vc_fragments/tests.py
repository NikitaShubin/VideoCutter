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