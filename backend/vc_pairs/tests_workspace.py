# -*- coding: utf-8 -*-
"""Тесты создания/удаления workspace (автономная обвязка через HTTP).

Прогон: cd backend && python manage.py test

Обвязка (``videocutter.standalone.workspace``) отвечает за ФС; здесь
проверяется тонкий HTTP-слой и откат при битом файле.
"""

import errno
import json
import os
import shutil
import time
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile

from vc_fragments.tests import WorkspaceApiTestBase
from videocutter.standalone import workspace as ws_fs

HTTP_CREATED = 201
HTTP_ACCEPTED = 202


def _wait_ready(testcase, ws_id, timeout=30):
    """Опрос detail, пока фоновая валидация после 202 (indexing)."""
    deadline = time.time() + timeout
    while True:
        body = testcase.client.get(f"/api/v1/workspaces/{ws_id}/").json()
        if not body.get("indexing") or body.get("broken"):
            return body
        if time.time() > deadline:
            testcase.fail(f"workspace {ws_id} never became ready")
        time.sleep(0.1)


def _wait_file(path, timeout=30):
    """Ждать появления файла (task.json пишет фоновая валидация)."""
    deadline = time.time() + timeout
    while not os.path.isfile(path):
        if time.time() > deadline:
            raise AssertionError(f"file never appeared: {path}")
        time.sleep(0.1)


class WorkspaceAdminTests(WorkspaceApiTestBase):
    def _video(self, name: str = "newvid.mp4") -> SimpleUploadedFile:
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def test_upload_creates_workspace(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "uploaded"})
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        body = resp.json()
        self.assertEqual(body["id"], "uploaded")
        # Метаданные догоняет фоновая валидация: ждём готовности.
        body = _wait_ready(self, "uploaded")
        self.assertFalse(body["broken"])
        self.assertGreater(body["total_frames"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self._tmpdir, "uploaded")))

        ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertIn("uploaded", ids)

    def test_upload_default_name_from_file(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video("clip42.mp4")})
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        self.assertEqual(resp.json()["id"], "clip42")

    def test_upload_missing_file(self):
        resp = self.client.post(self.ws_list_url, {"name": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_upload_rejects_non_video_extension(self):
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        resp = self.client.post(self.ws_list_url, {"file": bad, "name": "bad"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self._tmpdir, "bad")))

    def test_upload_invalid_video_reports_broken(self):
        """Битый «видеофайл»: 202, затем broken-запись с текстом (без отката)."""
        bad = SimpleUploadedFile("broken.mp4", b"not a video", content_type="video/mp4")
        resp = self.client.post(self.ws_list_url, {"file": bad, "name": "broken"})
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        body = _wait_ready(self, "broken")
        self.assertTrue(body["broken"])
        self.assertTrue(body["error"])
        # Каталог остаётся (видно в списке как битая задача).
        self.assertTrue(os.path.isdir(os.path.join(self._tmpdir, "broken")))

    def test_upload_disk_full_returns_507(self):
        """Кончилось место при записи: 507 с текстом вместо голого 500."""
        # logging.disable: тестовый harness (py3.14 + Django 4.2) падает
        # при логгинге любого 5xx-ответа — гасим логи на время проверки.
        import logging
        nospace = OSError(errno.ENOSPC, "No space left on device")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch(
                "vc_pairs.views.ws_fs.create_workspace_pair", side_effect=nospace
            ):
                resp = self.client.post(
                    self.ws_list_url, {"file": self._video(), "name": "big"}
                )
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)
        self.assertIn("места", resp.json()["error"])

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
        """Один файл — «нейтральный»: source == preview (как --preview в PVC),
        хранится под ролевым именем source.<ext> (расширение сохраняется)."""
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "mix"})
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        body = self.client.get("/api/v1/workspaces/mix/").json()
        self.assertEqual(body["source_name"], "source.mp4")
        self.assertEqual(body["preview_name"], "source.mp4")
        self.assertIsNone(body["unassigned_name"])
        ws_dir = os.path.join(self._tmpdir, "mix")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.mp4")))

    def test_upload_pair_with_roles(self):
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("result.mp4"),
            "preview": self._video("result.mp4"),
            "name": "pair",
        })
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
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
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        ws_dir = os.path.join(self._tmpdir, "unif")
        # task.json пишет фоновая валидация — ждём файл, затем сверяем состав.
        _wait_file(os.path.join(ws_dir, "task.json"))
        files = sorted(os.listdir(ws_dir))
        self.assertEqual(files, ["preview.mp4", "source.avi", "task.json"])
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
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
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
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
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

    def test_upload_role_adds_second_video_after_single(self):
        """Был один ролевой source — добавляем превью: получается пара."""
        self.client.post(self.ws_list_url, {"file": self._video("origin.mp4"), "name": "grow"})
        resp = self._upload_role(
            "/api/v1/workspaces/grow/video/preview/",
            file=self._video("marker.mp4"),
        )
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/grow/").json()
        self.assertEqual(body["source_name"], "source.mp4")
        self.assertEqual(body["preview_name"], "preview.mp4")
        ws_dir = os.path.join(self._tmpdir, "grow")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.mp4")))

    def test_upload_role_existing_promotes_legacy_neutral(self):
        """Legacy: одиночный файл под оригинальным именем + existing="source"
        становится source при добавлении превью (обратная совместимость)."""
        self.client.post(self.ws_list_url, {"file": self._video("legacy.mp4"), "name": "lgrow"})
        # Имитируем старый формат: файл лежит под оригинальным именем.
        ws_dir = os.path.join(self._tmpdir, "lgrow")
        os.rename(os.path.join(ws_dir, "source.mp4"), os.path.join(ws_dir, "legacy.mp4"))
        from workspace import get_workspace
        get_workspace("lgrow").invalidate_videos()
        resp = self._upload_role(
            "/api/v1/workspaces/lgrow/video/preview/",
            file=self._video("marker.mp4"),
            existing="source",
        )
        self.assertEqual(resp.status_code, 200)
        body = self.client.get("/api/v1/workspaces/lgrow/").json()
        self.assertEqual(body["source_name"], "source.mp4")
        self.assertEqual(body["preview_name"], "preview.mp4")

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

    def test_upload_role_disk_full_returns_507(self):
        """Кончилось место при замене роли: 507, старый файл роли цел."""
        import logging
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "keeproom",
        })
        nospace = OSError(errno.ENOSPC, "No space left on device")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch(
                "vc_pairs.views.ws_fs.write_stream", side_effect=nospace
            ):
                resp = self._upload_role(
                    "/api/v1/workspaces/keeproom/video/preview/",
                    file=self._video("c.mp4"),
                )
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)
        self.assertIn("места", resp.json()["error"])
        body = self.client.get("/api/v1/workspaces/keeproom/").json()
        self.assertEqual(body["preview_name"], "preview.mp4")
        self.assertTrue(
            os.path.isfile(os.path.join(self._tmpdir, "keeproom", "preview.mp4")))

    def test_upload_role_unknown_role(self):
        resp = self._upload_role("/api/v1/workspaces/x/video/bogus/", file=self._video())
        self.assertEqual(resp.status_code, 400)
        resp = self._upload_role("/api/v1/workspaces/nonexistent/video/source/", file=self._video())
        self.assertEqual(resp.status_code, 404)

    def test_upload_single_source_gets_role_name(self):
        resp = self.client.post(self.ws_list_url, {
            "source": self._video("clip.avi"),
            "name": "oners",
        })
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        body = self.client.get("/api/v1/workspaces/oners/").json()
        self.assertEqual(body["source_name"], "source.avi")
        self.assertEqual(body["preview_name"], "source.avi")
        ws_dir = os.path.join(self._tmpdir, "oners")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "source.avi")))

    def test_upload_single_preview_gets_role_name(self):
        resp = self.client.post(self.ws_list_url, {
            "preview": self._video("cam.webm"),
            "name": "onepv",
        })
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        body = self.client.get("/api/v1/workspaces/onepv/").json()
        self.assertEqual(body["preview_name"], "preview.webm")
        self.assertEqual(body["source_name"], "preview.webm")
        ws_dir = os.path.join(self._tmpdir, "onepv")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "preview.webm")))

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
        self.assertEqual(body["source_name"], "source.mp4")
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


class RenameApiTests(WorkspaceApiTestBase):
    """Переезд папки (PATCH .../ {"id": "new"}); отображаемое имя — {"name"}."""

    def test_rename_workspace(self):
        resp = self.client.patch(
            self.ws_detail_url,
            data='{"id": "renamed"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], "renamed")
        self.assertFalse(os.path.isdir(self.ws_dir))
        self.assertTrue(os.path.isdir(os.path.join(self._tmpdir, "renamed")))

    def test_rename_same_name_is_noop(self):
        resp = self.client.patch(
            self.ws_detail_url,
            data='{"id": "test-ws"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], "test-ws")

    def test_rename_conflict(self):
        other = os.path.join(self._tmpdir, "existing")
        os.makedirs(other, exist_ok=True)
        resp = self.client.patch(
            self.ws_detail_url,
            data='{"id": "existing"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_rename_invalid_name(self):
        resp = self.client.patch(
            self.ws_detail_url,
            data='{"id": "../evil"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_rename_missing_workspace(self):
        resp = self.client.patch(
            "/api/v1/workspaces/nonexistent/",
            data='{"id": "x"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_rename_preserves_data(self):
        """После переименования фрагменты и видео доступны по новому id."""
        resp = self.client.patch(
            self.ws_detail_url,
            data='{"id": "preserved"}',
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        frags = self.client.get("/api/v1/pairs/preserved/fragments/").json()
        self.assertEqual(len(frags), 2)
        self.assertGreater(self.client.get("/api/v1/workspaces/preserved/").json()["total_frames"], 0)

    def test_rename_blocked_while_export_running(self):
        """Переименование при активном экспорте — 409, каталог на месте."""
        from vc_fragments.views import EXPORTS, EXPORTS_LOCK

        with EXPORTS_LOCK:
            EXPORTS[self.ws_id] = {"state": "running", "index": 1, "total": 2}
        try:
            resp = self.client.patch(
                self.ws_detail_url,
                data='{"id": "renamed"}',
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 409)
            self.assertIn("Экспорт", resp.json()["error"])
            self.assertTrue(os.path.isdir(self.ws_dir))
        finally:
            with EXPORTS_LOCK:
                EXPORTS.pop(self.ws_id, None)


class DisplayNameApiTests(WorkspaceApiTestBase):
    """Отображаемое имя (PATCH {"name"}): дубли разрешены, папка на месте."""

    def _patch(self, payload, ws_id=None):
        url = f"/api/v1/workspaces/{ws_id or self.ws_id}/"
        return self.client.patch(
            url, data=json.dumps(payload), content_type="application/json")

    def test_name_sets_display_without_moving(self):
        resp = self._patch({"name": "Мой выезд"})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["id"], self.ws_id)
        self.assertEqual(body["name"], "Мой выезд")
        self.assertTrue(os.path.isdir(self.ws_dir))
        items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
        self.assertEqual(items[self.ws_id]["name"], "Мой выезд")

    def test_names_may_duplicate_across_tasks(self):
        """Как в CVAT: одинаковые имена задач — нормально (уникален id)."""
        other = os.path.join(self._tmpdir, "other-ws")
        os.makedirs(other, exist_ok=True)
        shutil.copy2(self.video, os.path.join(other, "video.mp4"))
        for ws_id in (self.ws_id, "other-ws"):
            resp = self._patch({"name": "Одинаковое"}, ws_id=ws_id)
            self.assertEqual(resp.status_code, 200, resp.content)
        names = {w["id"]: w["name"]
                 for w in self.client.get(self.ws_list_url).json()}
        self.assertEqual(names[self.ws_id], "Одинаковое")
        self.assertEqual(names["other-ws"], "Одинаковое")

    def test_name_empty_falls_back_to_id(self):
        self.assertEqual(
            self.client.get(self.ws_detail_url).json()["name"], self.ws_id)

    def test_name_rejects_non_string(self):
        resp = self._patch({"name": 42})
        self.assertEqual(resp.status_code, 400)

    def test_name_missing_workspace_404(self):
        resp = self._patch({"name": "x"}, ws_id="nope")
        self.assertEqual(resp.status_code, 404)


class ListRobustnessTests(WorkspaceApiTestBase):
    """Список переживает битые задачи и долгую индексацию (F2/F3)."""

    def _item(self, ws_id):
        items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
        return items[ws_id]

    def test_creating_set_hides_workspace(self):
        """note_creating прячет недозалитую задачу; clear/stale — показывают."""
        import workspace as ws_module

        hidden = os.path.join(self._tmpdir, "hidden-ws")
        os.makedirs(hidden, exist_ok=True)
        shutil.copy2(self.video, os.path.join(hidden, "video.mp4"))
        ws_module.note_creating("hidden-ws")
        try:
            ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
            self.assertNotIn("hidden-ws", ids)
            ws_module.clear_creating("hidden-ws")
            ids2 = [w["id"] for w in self.client.get(self.ws_list_url).json()]
            self.assertIn("hidden-ws", ids2)
            # Протухшая отметка — зависшая заливка видна (можно удалить).
            with ws_module._creating_lock:
                ws_module._creating["hidden-ws"] = time.time() - 3600
            ids3 = [w["id"] for w in self.client.get(self.ws_list_url).json()]
            self.assertIn("hidden-ws", ids3)
        finally:
            ws_module.clear_creating("hidden-ws")

    def test_broken_workspace_isolated(self):
        """Битая задача — записью broken, остальные — целы, список — 200."""
        broken_dir = os.path.join(self._tmpdir, "broken-ws")
        os.makedirs(broken_dir, exist_ok=True)
        with open(os.path.join(broken_dir, "source.mp4"), "wb") as f:
            f.write(b"not a video")

        deadline = time.time() + 15
        bad = None
        while True:
            resp = self.client.get(self.ws_list_url)
            self.assertEqual(resp.status_code, 200)
            items = {w["id"]: w for w in resp.json()}
            # Хорошая задача не пострадала.
            self.assertIn(self.ws_id, items)
            self.assertFalse(items[self.ws_id]["broken"])
            bad = items.get("broken-ws")
            self.assertIsNotNone(bad)
            if bad["broken"]:
                break
            # Первый проход: индекс ещё строится в фоне.
            self.assertTrue(bad["indexing"])
            if time.time() > deadline:
                self.fail("broken workspace never reported as broken")
            time.sleep(0.05)
        self.assertTrue(bad["error"])

    def test_indexing_then_ready(self):
        """Новая задача: сначала indexing, затем готовые метрики (фон)."""
        fresh = os.path.join(self._tmpdir, "fresh-ws")
        os.makedirs(fresh, exist_ok=True)
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        shutil.copy2(src, os.path.join(fresh, "source.mp4"))

        first = self._item("fresh-ws")
        self.assertTrue(first["indexing"])
        self.assertFalse(first["broken"])

        deadline = time.time() + 30
        while True:
            item = self._item("fresh-ws")
            if not item["indexing"]:
                break
            if time.time() > deadline:
                self.fail("background index never finished")
            time.sleep(0.1)
        self.assertFalse(item["broken"])
        self.assertGreater(item["total_frames"], 0)


class DeleteRobustnessTests(WorkspaceApiTestBase):
    """Удаление: причина в ответе, отмена фона экспорта (F7)."""

    def test_delete_during_validation_succeeds(self):
        """DELETE во время фоновой валидации: 200, каталог ушёл, валидация
        тихо вышла (без broken-записи — показывать нечего и некому)."""
        import threading

        import workspace as ws_module
        import vc_pairs.views as views
        from vc_pairs import frame_provider as fp_module

        entered = threading.Event()
        release = threading.Event()
        real_get_metadata = fp_module.get_metadata

        def blocked(path):
            entered.set()
            self.assertTrue(release.wait(timeout=30))
            return real_get_metadata(path)

        with mock.patch.object(fp_module, "get_metadata", side_effect=blocked):
            t = threading.Thread(
                target=views._validate_workspace_async,
                args=(self.ws_id,), daemon=True)
            t.start()
            self.assertTrue(entered.wait(timeout=30))
            resp = self.client.delete(self.ws_detail_url)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(os.path.exists(self.ws_dir))
            release.set()
            t.join(timeout=30)
        self.assertFalse(t.is_alive(), "валидация не вышла после удаления")
        self.assertIsNone(ws_module.workspace_error(self.ws_id))

    def test_delete_oserror_returns_500_with_reason(self):
        import logging

        import vc_pairs.views as views

        # logging.disable: см. test_upload_disk_full_returns_507 — harness
        # падает при логгинге 5xx на py3.14 + Django 4.2.
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(
                ws_fs, "delete_workspace", side_effect=OSError("busy")
            ), mock.patch.object(views, "_DELETE_ATTEMPTS", 2), mock.patch.object(
                views, "_DELETE_RETRY_DELAY", 0
            ):
                resp = self.client.delete(self.ws_detail_url)
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 500)
        self.assertIn("busy", resp.json()["error"])

    def test_delete_requests_export_cancel(self):
        from vc_fragments.views import EXPORTS, EXPORTS_LOCK, EXPORT_CANCEL

        with EXPORTS_LOCK:
            EXPORTS[self.ws_id] = {"state": "running", "index": 1, "total": 2}
        try:
            resp = self.client.delete(self.ws_detail_url)
            self.assertEqual(resp.status_code, 200)
            with EXPORTS_LOCK:
                self.assertIn(self.ws_id, EXPORT_CANCEL)
        finally:
            with EXPORTS_LOCK:
                EXPORTS.pop(self.ws_id, None)
                EXPORT_CANCEL.discard(self.ws_id)
