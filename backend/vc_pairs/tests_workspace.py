# -*- coding: utf-8 -*-
"""Тесты создания/удаления workspace (автономная обвязка через HTTP).

Прогон: cd backend && python manage.py test

Обвязка (``videocutter.standalone.workspace``) отвечает за ФС; здесь
проверяется тонкий HTTP-слой и откат при битом файле.
"""

import os

from django.core.files.uploadedfile import SimpleUploadedFile

from vc_fragments.tests import WorkspaceApiTestBase

HTTP_CREATED = 201


class WorkspaceAdminTests(WorkspaceApiTestBase):
    def _video(self, name: str = "newvid.mp4") -> SimpleUploadedFile:
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def test_upload_creates_workspace(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "uploaded"})
        self.assertEqual(resp.status_code, HTTP_CREATED)
        body = resp.json()
        self.assertEqual(body["id"], "uploaded")
        self.assertGreater(body["total_frames"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self._tmpdir, "uploaded")))

        ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertIn("uploaded", ids)

    def test_upload_default_name_from_file(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video("clip42.mp4")})
        self.assertEqual(resp.status_code, HTTP_CREATED)
        self.assertEqual(resp.json()["id"], "clip42")

    def test_upload_missing_file(self):
        resp = self.client.post(self.ws_list_url, {"name": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_upload_rejects_non_video_extension(self):
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post(self.ws_list_url, {"file": bad, "name": "bad"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self._tmpdir, "bad")))

    def test_upload_rolls_back_invalid_video(self):
        """Битый «видеофайл» не оставляет частичный workspace."""
        bad = SimpleUploadedFile("broken.mp4", b"not a video", content_type="video/mp4")
        resp = self.client.post(self.ws_list_url, {"file": bad, "name": "broken"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self._tmpdir, "broken")))

    def test_upload_duplicate_name_conflict(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": self.ws_id})
        self.assertEqual(resp.status_code, 409)

    def test_upload_rejects_name_with_path(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "../evil"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self._tmpdir), "evil")))

    def test_delete_workspace(self):
        resp = self.client.delete(self.ws_detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], self.ws_id)
        self.assertFalse(os.path.isdir(self.ws_dir))

        ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertNotIn(self.ws_id, ids)

    def test_delete_missing_workspace(self):
        resp = self.client.delete("/api/v1/workspaces/nonexistent/")
        self.assertEqual(resp.status_code, 404)
