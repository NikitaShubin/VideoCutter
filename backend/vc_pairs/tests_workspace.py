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


class RoleApiTests(WorkspaceApiTestBase):
    """Роли source/preview: создание пары, замена, swap, смена нейтрального."""

    def _video(self, name: str = "vid.mp4") -> SimpleUploadedFile:
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def _upload_role(self, url, **fields):
        # Django 4.2 не заполняет request.FILES для PUT/PATCH, поэтому аплоад роли
        # идёт POST'ом (как и создание workspace).
        return self.client.post(url, fields)

    def test_upload_single_is_neutral(self):
        """Один файл — «нейтральный»: source == preview (как --preview в PVC)."""
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "mix"})
        self.assertEqual(resp.status_code, HTTP_CREATED)
        body = self.client.get("/api/v1/workspaces/mix/").json()
        self.assertEqual(body["source_name"], "vid.mp4")
        self.assertEqual(body["preview_name"], "vid.mp4")
        self.assertIsNone(body["unassigned_name"])

    def test_upload_pair_with_roles(self):
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("result.mp4"),
            "preview": self._video("result.mp4"),
            "name": "pair",
        })
        self.assertEqual(resp.status_code, HTTP_CREATED)
        body = self.client.get("/api/v1/workspaces/pair/").json()
        self.assertEqual(body["source_name"], "source.mp4")
        self.assertEqual(body["preview_name"], "preview.mp4")
        self.assertGreater(body["total_frames"], 0)
        ws_dir = os.path.join(self._tmpdir, "pair")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.mp4")))

    def test_upload_pair_unified_names_ignore_original(self):
        """Унификация: имена файлов = роли, оригинальные имена не сохраняются."""
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("6.avi"),
            "preview": self._video("6_preview.mp4"),
            "name": "unif",
        })
        self.assertEqual(resp.status_code, HTTP_CREATED)
        ws_dir = os.path.join(self._tmpdir, "unif")
        files = sorted(os.listdir(ws_dir))
        self.assertEqual(files, ["preview.mp4", "source.avi"])
        body = self.client.get("/api/v1/workspaces/unif/").json()
        self.assertEqual(body["source_name"], "source.avi")
        self.assertEqual(body["preview_name"], "preview.mp4")

    def test_upload_pair_identical_filenames_stored_separately(self):
        """Два видео с одинаковыми именами не конфликтуют (роль = имя файла)."""
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("same.mp4"),
            "preview": self._video("same.mp4"),
            "name": "samepair",
        })
        self.assertEqual(resp.status_code, HTTP_CREATED)
        ws_dir = os.path.join(self._tmpdir, "samepair")
        files = sorted(os.listdir(ws_dir))
        self.assertIn("source.mp4", files)
        self.assertIn("preview.mp4", files)

    def test_upload_accepts_non_mp4_extension(self):
        """Любое видео: расширение не ограничено mp4 (валидация по содержимому)."""
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("clip.m2ts"),
            "name": "anyfmt",
        })
        self.assertEqual(resp.status_code, HTTP_CREATED)
        self.assertGreater(self.client.get("/api/v1/workspaces/anyfmt/").json()["total_frames"], 0)

    def test_swap_roles(self):
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "sw",
        })
        resp = self.client.post("/api/v1/workspaces/sw/swap/")
        self.assertEqual(resp.status_code, 200)
        after = self.client.get("/api/v1/workspaces/sw/").json()
        # Имена ролей унифицированы: после swap роли «обмениваются» файлами.
        self.assertEqual(after["source_name"], "source.mp4")
        self.assertEqual(after["preview_name"], "preview.mp4")
        ws_dir = os.path.join(self._tmpdir, "sw")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.mp4")))

    def test_swap_requires_two_videos(self):
        self.client.post(self.ws_list_url, {"file": self._video(), "name": "sw1"})
        resp = self.client.post("/api/v1/workspaces/sw1/swap/")
        self.assertEqual(resp.status_code, 400)

    def test_upload_role_adds_second_video_with_existing_role(self):
        """Был один нейтральный файл — добавляем превью: файл становится source."""
        self.client.post(self.ws_list_url, {"file": self._video("origin.mp4"), "name": "grow"})
        resp = self._upload_role(
            "/api/v1/workspaces/grow/video/preview/",
            file=self._video("marker.mp4"),
            existing="source",
        )
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/grow/").json()
        self.assertEqual(body["source_name"], "source.mp4")
        self.assertEqual(body["preview_name"], "preview.mp4")
        ws_dir = os.path.join(self._tmpdir, "grow")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.mp4")))

    def test_upload_role_replace_removes_old_file(self):
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "repl",
        })
        resp = self._upload_role(
            "/api/v1/workspaces/repl/video/preview/",
            file=self._video("c.mp4"),
        )
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/repl/").json()
        self.assertEqual(body["preview_name"], "preview.mp4")
        self.assertEqual(body["source_name"], "source.mp4")
        ws_dir = os.path.join(self._tmpdir, "repl")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.mp4")))
        with open(os.path.join(ws_dir, "preview.mp4"), "rb") as f:
            self.assertEqual(f.read(), self._test_video_bytes())

    def _test_video_bytes(self):
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            return f.read()

    def test_upload_role_invalid_video_preserves_role(self):
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "keep",
        })
        bad = SimpleUploadedFile("bad.mp4", b"not a video", content_type="video/mp4")
        resp = self._upload_role("/api/v1/workspaces/keep/video/preview/", file=bad)
        self.assertEqual(resp.status_code, 400)
        body = self.client.get("/api/v1/workspaces/keep/").json()
        self.assertEqual(body["preview_name"], "preview.mp4")
        self.assertTrue(os.path.isfile(os.path.join(self._tmpdir, "keep", "preview.mp4")))

    def test_upload_role_unknown_role(self):
        resp = self._upload_role("/api/v1/workspaces/x/video/bogus/", file=self._video())
        self.assertEqual(resp.status_code, 400)
        resp = self._upload_role("/api/v1/workspaces/nonexistent/video/source/", file=self._video())
        self.assertEqual(resp.status_code, 404)

    def test_assign_unassigned_video_to_role(self):
        """Неразмеченный файл (unassigned) назначается роли без новой загрузки."""
        self.client.post(self.ws_list_url, {"file": self._video("aaa.mp4"), "name": "asg"})
        # Кладём второй «нейтральный» файл напрямую (ситуация двух нейтральных):
        # первый в алфавитном порядке остаётся нейтральным source/preview.
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        ws_dir = os.path.join(self._tmpdir, "asg")
        with open(src, "rb") as f, open(os.path.join(ws_dir, "zzz.mp4"), "wb") as o:
            o.write(f.read())
        # Сброс кэша ролей (в проде это делает backend после операций с файлами).
        from workspace import get_workspace
        get_workspace("asg").invalidate_videos()
        body = self.client.get("/api/v1/workspaces/asg/").json()
        self.assertEqual(body["unassigned_name"], "zzz.mp4")

        resp = self.client.post("/api/v1/workspaces/asg/video/preview/", {"assign": "zzz.mp4"})
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/asg/").json()
        self.assertEqual(body["preview_name"], "preview.mp4")
        self.assertEqual(body["source_name"], "aaa.mp4")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.mp4")))

    def test_assign_conflicts_with_upload(self):
        self.client.post(self.ws_list_url, {"file": self._video(), "name": "asgc"})
        resp = self.client.post("/api/v1/workspaces/asgc/video/preview/",
                                {"assign": "e.mp4", "file": self._video()})
        self.assertEqual(resp.status_code, 400)

    def test_delete_role_back_to_single(self):
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "delr",
        })
        resp = self.client.delete("/api/v1/workspaces/delr/video/preview/")
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/delr/").json()
        self.assertFalse(os.path.isfile(os.path.join(self._tmpdir, "delr", "preview.mp4")))
        # После удаления превью оставшееся видео — единственный источник.
        self.assertEqual(body["source_name"], body["preview_name"])
        self.assertEqual(body["source_name"], "source.mp4")
        # Нельзя удалить единственное видео.
        resp = self.client.delete("/api/v1/workspaces/delr/video/source/")
        self.assertEqual(resp.status_code, 400)
