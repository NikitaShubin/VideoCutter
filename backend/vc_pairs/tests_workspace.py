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


class LightStatusTests(WorkspaceApiTestBase):
    """GET ?light=1: волатильное состояние для тика — без TSV и метрик."""

    def test_light_shape_and_parity(self):
        full = {w["id"] for w in self.client.get(self.ws_list_url).json()}
        resp = self.client.get(self.ws_list_url + "?light=1")
        self.assertEqual(resp.status_code, 200)
        light = resp.json()
        self.assertEqual({w["id"] for w in light}, full)
        for w in light:
            self.assertEqual(
                set(w.keys()),
                {"id", "indexing", "indexing_progress", "export",
                 "updated_at", "broken", "error", "error_role"})
            self.assertNotIn("fragments", w)


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


class BrokenRoleTests(WorkspaceApiTestBase):
    """Битая задача: видно, КАКОЙ файл не читается (``error_role``).

    Пользователь должен сразу понять, что заменять: превью, источник или
    оба. Роль отдаёт бэкенд — он знает, какой файл не открылся.
    """

    def _make_ws(self, name: str, broken=()) -> str:
        """Workspace с source.mp4 и preview.mp4; broken — роли с мусором."""
        d = os.path.join(self._tmpdir, name)
        os.makedirs(d, exist_ok=True)
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        shutil.copy2(src, os.path.join(d, "source.mp4"))
        shutil.copy2(src, os.path.join(d, "preview.mp4"))
        for role in broken:
            with open(os.path.join(d, f"{role}.mp4"), "wb") as f:
                f.write(b"not a video")
        return d

    def _wait_role(self, name: str, expected=None, timeout: float = 30):
        """Ждём broken-запись (и роль, если задана).

        Роль выводится из файлов: пока фоновые сборки индексов не отработали
        для всех ролей, она проявится не сразу — ждём нужного значения.
        """
        deadline = time.time() + timeout
        while True:
            items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
            item = items.get(name)
            self.assertIsNotNone(item)
            if item["broken"] and (expected is None or item["error_role"] == expected):
                return item
            if time.time() > deadline:
                self.fail(
                    f"workspace {name}: ждали роль {expected!r}, "
                    f"получили broken={item['broken']} role={item['error_role']!r}"
                )
            time.sleep(0.1)

    def test_broken_preview_reports_preview_role(self):
        self._make_ws("bad-preview", broken=("preview",))
        item = self._wait_role("bad-preview", "preview")
        self.assertEqual(item["error_role"], "preview")

    def test_broken_source_reports_source_role(self):
        self._make_ws("bad-source", broken=("source",))
        item = self._wait_role("bad-source", "source")
        self.assertEqual(item["error_role"], "source")

    def test_broken_both_reports_both_roles(self):
        self._make_ws("bad-both", broken=("source", "preview"))
        item = self._wait_role("bad-both", "both")
        self.assertEqual(item["error_role"], "both")

    def test_error_without_file_has_no_role(self):
        """Сбой не про файл (например, проверка) — роль не угадываем."""
        import workspace as ws_module

        self._make_ws("no-role-ws")
        ws_module.note_workspace_error("no-role-ws", "что-то сломалось")
        try:
            items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
            self.assertTrue(items["no-role-ws"]["broken"])
            self.assertIsNone(items["no-role-ws"]["error_role"])
        finally:
            ws_module.clear_workspace_error("no-role-ws")

    def test_broken_upload_pair_reports_both_roles(self):
        """Залили два битых файла — сломаны обе роли (не только первая)."""
        resp = self.client.post(
            self.ws_list_url,
            {"name": "bad-pair", "source": SimpleUploadedFile(
                "source.mp4", b"not a video", content_type="video/mp4"),
             "preview": SimpleUploadedFile(
                "preview.mp4", b"not a video either", content_type="video/mp4")},
        )
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        item = self._wait_role("bad-pair", "both")
        self.assertEqual(item["error_role"], "both")

    def test_healthy_task_has_no_role(self):
        items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
        self.assertIsNone(items[self.ws_id]["error_role"])

    def test_empty_path_is_not_a_role(self):
        """Пустой путь — не роль (у задачи без видео роли пустые)."""
        import workspace as ws_module
        d = os.path.join(self._tmpdir, "empty-ws")
        os.makedirs(d, exist_ok=True)
        ws = ws_module.Workspace(name="empty-ws", path=d)
        self.assertEqual(ws_module.roles_of_paths(ws, [""]), "")


class BrokenRecoveryTests(WorkspaceApiTestBase):
    """Починка файла снимает поломку: правда — на диске, а не в памяти.

    «Правило трубы»: состояние выводится из файлов. Если сломанный файл
    заменили рабочим, задача обязана перестать быть битой без перезапуска
    процесса — память не должна помнить поломку, которой на диске уже нет.
    """

    def _video(self, name="v.mp4") -> SimpleUploadedFile:
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def _garbage(self, name="v.mp4") -> SimpleUploadedFile:
        return SimpleUploadedFile(name, b"not a video at all", content_type="video/mp4")

    def _wait_broken(self, name: str, timeout: float = 60):
        deadline = time.time() + timeout
        while True:
            items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
            item = items.get(name)
            self.assertIsNotNone(item)
            if item["broken"]:
                return item
            if time.time() > deadline:
                self.fail(f"workspace {name} never reported as broken")
            time.sleep(0.1)

    def _wait_healthy(self, name: str, timeout: float = 30):
        deadline = time.time() + timeout
        while True:
            items = {w["id"]: w for w in self.client.get(self.ws_list_url).json()}
            item = items.get(name)
            self.assertIsNotNone(item)
            if not item["broken"] and not item["indexing"]:
                return item
            if time.time() > deadline:
                self.fail(f"workspace {name} never recovered: {item}")
            time.sleep(0.2)

    def test_replacing_broken_upload_recovers(self):
        """Залили битое превью (ошибка проверки живёт в памяти) → заменили."""
        resp = self.client.post(
            self.ws_list_url,
            {"name": "broken-up", "source": self._video("source.mp4"),
             "preview": self._garbage("preview.mp4")},
        )
        self.assertEqual(resp.status_code, HTTP_ACCEPTED)
        self._wait_broken("broken-up")

        resp = self.client.post(
            "/api/v1/workspaces/broken-up/video/preview/",
            {"file": self._video("preview.mp4")},
        )
        self.assertEqual(resp.status_code, 200)

        item = self._wait_healthy("broken-up")
        self.assertIsNone(item["error_role"])

    def test_replacing_broken_file_on_disk_recovers(self):
        """Битый файл на диске (ошибка сборки индекса) → заменили → ожил."""
        d = os.path.join(self._tmpdir, "broken-disk")
        os.makedirs(d, exist_ok=True)
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        shutil.copy2(src, os.path.join(d, "source.mp4"))
        with open(os.path.join(d, "preview.mp4"), "wb") as f:
            f.write(b"not a video at all")

        self._wait_broken("broken-disk")

        resp = self.client.post(
            "/api/v1/workspaces/broken-disk/video/preview/",
            {"file": self._video("preview.mp4")},
        )
        self.assertEqual(resp.status_code, 200)

        item = self._wait_healthy("broken-disk")
        self.assertIsNone(item["error_role"])


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


class _CovHelperMixin:
    def _video(self, name="vid.mp4"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        src = os.path.join(os.path.dirname(__file__), "..", "testdata", "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        return SimpleUploadedFile(name, data, content_type="video/mp4")


class StampCoverageTests(WorkspaceApiTestBase):
    def test_stamp_opened_swallows_load_error(self):
        import vc_pairs.views as views
        with mock.patch.object(views.task_meta, "load", side_effect=OSError("boom")):
            resp = self.client.get(self.ws_detail_url)
        self.assertEqual(resp.status_code, 200)

    def test_stamp_opened_swallows_save_error(self):
        import vc_pairs.views as views
        with mock.patch.object(views.task_meta, "save", side_effect=RuntimeError("boom")):
            resp = self.client.get(self.ws_detail_url)
        self.assertEqual(resp.status_code, 200)

    def test_stamp_opened_missing_ws_is_noop(self):
        import vc_pairs.views as views
        views._stamp_opened("no-such-ws-zzz")


class ValidateCoverageTests(WorkspaceApiTestBase):
    def test_validate_missing_ws_quiet(self):
        import workspace as ws_module
        import vc_pairs.views as views
        views._validate_workspace_async("no-such-ws-zzz")
        self.assertIsNone(ws_module.workspace_error("no-such-ws-zzz"))

    def test_validate_no_video_marks_broken(self):
        import workspace as ws_module
        import vc_pairs.views as views
        empty = os.path.join(self._tmpdir, "empty-ws")
        os.makedirs(empty, exist_ok=True)
        views._validate_workspace_async("empty-ws")
        err = ws_module.workspace_error("empty-ws")
        self.assertIsNotNone(err)
        self.assertIn("не распознано", err)

    def test_validate_zero_frames_marks_broken(self):
        import workspace as ws_module
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_metadata",
                               return_value={"total_frames": 0}):
            views._validate_workspace_async(self.ws_id)
        err = ws_module.workspace_error(self.ws_id)
        self.assertIsNotNone(err)
        self.assertIn("кадров", err)

    def test_validate_decode_cancelled_quiet(self):
        import workspace as ws_module
        import vc_pairs.views as views
        from vc_pairs import frame_provider as fp
        with mock.patch.object(views.frame_provider, "get_metadata",
                               side_effect=fp._DecodeCancelled("gone")):
            views._validate_workspace_async(self.ws_id)
        self.assertIsNone(ws_module.workspace_error(self.ws_id))

    def test_validate_oserror_marks_broken(self):
        import workspace as ws_module
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_metadata",
                               side_effect=OSError(errno.ENOSPC, "No space")):
            views._validate_workspace_async(self.ws_id)
        err = ws_module.workspace_error(self.ws_id)
        self.assertIsNotNone(err)
        self.assertIn("места", err)

    def test_validate_generic_error_marks_broken(self):
        import workspace as ws_module
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_metadata",
                               side_effect=RuntimeError("bad header")):
            views._validate_workspace_async(self.ws_id)
        err = ws_module.workspace_error(self.ws_id)
        self.assertIsNotNone(err)
        self.assertIn("Не удалось прочитать", err)

    def test_validate_save_failure_swallowed(self):
        import workspace as ws_module
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_metadata",
                               return_value={"total_frames": 10}), \
             mock.patch.object(views.task_meta, "save", side_effect=OSError("disk")):
            views._validate_workspace_async(self.ws_id)
        self.assertIsNone(ws_module.workspace_error(self.ws_id))


class UploadCoverageTests(_CovHelperMixin, WorkspaceApiTestBase):
    def test_upload_race_exists_409(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "create_workspace_pair",
                               side_effect=ws_fs_mod.WorkspaceExistsError("уже есть")):
            resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "race"})
        self.assertEqual(resp.status_code, 409)

    def test_upload_invalid_workspace_400(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "create_workspace_pair",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("плохое имя")):
            resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "badx"})
        self.assertEqual(resp.status_code, 400)

    def test_upload_unrecognized_rolls_back_400(self):
        import vc_pairs.views as views

        def _fake_create(root, name, source=None, preview=None):
            dest = os.path.join(root, name)
            os.makedirs(dest, exist_ok=True)
            return dest
        with mock.patch.object(views.ws_fs, "create_workspace_pair", side_effect=_fake_create):
            resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "novid"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("не распознано", resp.json()["error"])
        self.assertFalse(os.path.isdir(os.path.join(self._tmpdir, "novid")))

    def test_upload_unrecognized_rollback_failure_still_400(self):
        import vc_pairs.views as views

        def _fake_create(root, name, source=None, preview=None):
            dest = os.path.join(root, name)
            os.makedirs(dest, exist_ok=True)
            return dest
        with mock.patch.object(views.ws_fs, "create_workspace_pair", side_effect=_fake_create), \
             mock.patch.object(views.ws_fs, "delete_workspace", side_effect=OSError("busy")):
            resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "novid2"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("не распознано", resp.json()["error"])

    def test_upload_reserved_projects_name_400(self):
        resp = self.client.post(self.ws_list_url, {"file": self._video(), "name": "projects"})
        self.assertEqual(resp.status_code, 400)


class RenameCoverageTests(WorkspaceApiTestBase):
    def _patch(self, payload_raw):
        return self.client.patch(
            self.ws_detail_url, data=payload_raw, content_type="application/json")

    def test_rename_bad_json_400(self):
        resp = self._patch("{bad json")
        self.assertEqual(resp.status_code, 400)

    def test_rename_non_dict_400(self):
        resp = self._patch("[1, 2]")
        self.assertEqual(resp.status_code, 400)

    def test_rename_display_value_error_400(self):
        import vc_pairs.views as views
        with mock.patch.object(views.task_meta, "save", side_effect=ValueError("плохой статус")):
            resp = self.client.patch(
                self.ws_detail_url, data=json.dumps({"name": "X"}),
                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_rename_display_oserror_507(self):
        import logging
        import vc_pairs.views as views
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(views.task_meta, "save", side_effect=OSError("disk")):
                resp = self.client.patch(
                    self.ws_detail_url, data=json.dumps({"name": "X"}),
                    content_type="application/json")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)

    def test_rename_id_non_string_400(self):
        resp = self.client.patch(
            self.ws_detail_url, data=json.dumps({"id": 42}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_rename_id_reserved_400(self):
        resp = self.client.patch(
            self.ws_detail_url, data=json.dumps({"id": "projects"}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_rename_id_not_found_404(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "rename_workspace",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("Workspace 'x' не найден")):
            resp = self.client.patch(
                self.ws_detail_url, data=json.dumps({"id": "newname"}),
                content_type="application/json")
        self.assertEqual(resp.status_code, 404)

    def test_rename_id_invalid_400(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "rename_workspace",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("Некорректное имя")):
            resp = self.client.patch(
                self.ws_detail_url, data=json.dumps({"id": "newname"}),
                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_rename_moves_export_state(self):
        from vc_fragments.views import EXPORTS, EXPORTS_LOCK
        with EXPORTS_LOCK:
            EXPORTS[self.ws_id] = {"state": "done", "index": 2, "total": 2}
        try:
            resp = self.client.patch(
                self.ws_detail_url, data=json.dumps({"id": "moved-ws"}),
                content_type="application/json")
            self.assertEqual(resp.status_code, 200)
            with EXPORTS_LOCK:
                self.assertNotIn(self.ws_id, EXPORTS)
                self.assertIn("moved-ws", EXPORTS)
        finally:
            with EXPORTS_LOCK:
                EXPORTS.pop(self.ws_id, None)
                EXPORTS.pop("moved-ws", None)


class DeleteCoverageTests(WorkspaceApiTestBase):
    def test_delete_invalid_workspace_404(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "delete_workspace",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("не найден")):
            resp = self.client.delete(self.ws_detail_url)
        self.assertEqual(resp.status_code, 404)


class FrameCoverageTests(WorkspaceApiTestBase):
    def test_frame_missing_ws_404(self):
        resp = self.client.get("/api/v1/workspaces/nonexistent/frame/0/")
        self.assertEqual(resp.status_code, 404)

    def test_frame_no_video_404(self):
        empty = os.path.join(self._tmpdir, "novid-ws")
        os.makedirs(empty, exist_ok=True)
        resp = self.client.get("/api/v1/workspaces/novid-ws/frame/0/")
        self.assertEqual(resp.status_code, 404)

    def test_frame_bad_quality_400(self):
        resp = self.client.get(self.ws_frame_url + "?quality=bad")
        self.assertEqual(resp.status_code, 400)

    def test_frame_bad_scale_400(self):
        resp = self.client.get(self.ws_frame_url + "?scale=bad")
        self.assertEqual(resp.status_code, 400)

    def test_frame_cancelled_404(self):
        import vc_pairs.views as views
        from vc_pairs import frame_provider as fp
        with mock.patch.object(views.frame_provider, "get_frame_jpeg",
                               side_effect=fp._DecodeCancelled("gone")):
            resp = self.client.get(self.ws_frame_url)
        self.assertEqual(resp.status_code, 404)

    def test_frame_unavailable_404(self):
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_frame_jpeg",
                               return_value=(None, None, {"source": "?"})):
            resp = self.client.get(self.ws_frame_url)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("недоступен", resp.json()["error"])

    def test_frame_ok(self):
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_frame_jpeg",
                               return_value=(b"fakejpeg", "image/jpeg", {"source": "cache"})):
            resp = self.client.get(self.ws_frame_url + "?quality=50&scale=0.5")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/jpeg")
        self.assertEqual(resp["X-Cache"], "cache")
        self.assertIn("X-Decode-Ms", resp)
        self.assertEqual(resp["Cache-Control"], "private, max-age=3600")
        self.assertNotIn("X-Abandoned", resp)

    def test_frame_ok_abandoned(self):
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_frame_jpeg",
                               return_value=(b"fakejpeg", "image/jpeg",
                                             {"source": "decode", "abandoned": True})):
            resp = self.client.get(self.ws_frame_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Abandoned"], "1")

    def test_frame_original_kind(self):
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_frame_jpeg",
                               return_value=(b"fakejpeg", "image/jpeg", {"source": "cache"})) as m:
            resp = self.client.get(self.ws_frame_url + "?video=original")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(m.called)


class CacheCoverageTests(WorkspaceApiTestBase):
    def test_cache_bad_json_400(self):
        resp = self.client.post("/api/v1/cache/", data="{bad", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_cache_invalid_caps_400(self):
        resp = self.client.post(
            "/api/v1/cache/", data=json.dumps({"gops": 0}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_cache_get_ok(self):
        resp = self.client.get("/api/v1/cache/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("caps", resp.json())

    def test_debug_threads_ok(self):
        resp = self.client.get("/api/v1/debug/threads")
        self.assertEqual(resp.status_code, 200)

    def test_meta_missing_404(self):
        resp = self.client.get("/api/v1/workspaces/nonexistent/meta")
        self.assertEqual(resp.status_code, 404)

    def test_upload_status_bad_id_404(self):
        resp = self.client.get("/api/v1/uploads/bad id!/status")
        self.assertEqual(resp.status_code, 404)

    def test_upload_status_missing_404(self):
        resp = self.client.get("/api/v1/uploads/0123456789abcdef0123456789abcdef/status")
        self.assertEqual(resp.status_code, 404)


class RoleCoverageTests(_CovHelperMixin, WorkspaceApiTestBase):
    def test_assign_invalid_file_400(self):
        resp = self.client.post(f"/api/v1/workspaces/{self.ws_id}/video/preview/",
                                {"assign": "gone.mp4"})
        self.assertEqual(resp.status_code, 400)

    def test_role_missing_file_400(self):
        resp = self.client.post(f"/api/v1/workspaces/{self.ws_id}/video/preview/", {})
        self.assertEqual(resp.status_code, 400)

    def test_role_existing_unknown_400(self):
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws_id}/video/preview/",
            {"file": self._video(), "existing": "bogus"})
        self.assertEqual(resp.status_code, 400)

    def test_role_existing_equals_role_400(self):
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws_id}/video/source/",
            {"file": self._video(), "existing": "source"})
        self.assertEqual(resp.status_code, 400)

    def test_role_zero_frames_400(self):
        import vc_pairs.views as views
        with mock.patch.object(views.frame_provider, "get_metadata",
                               return_value={"total_frames": 0}):
            resp = self.client.post(
                f"/api/v1/workspaces/{self.ws_id}/video/preview/",
                {"file": self._video()})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("кадров", resp.json()["error"])

    def test_role_invalid_content_400(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        bad = SimpleUploadedFile("bad.mp4", b"not a video", content_type="video/mp4")
        resp = self.client.post(
            f"/api/v1/workspaces/{self.ws_id}/video/preview/", {"file": bad})
        self.assertEqual(resp.status_code, 400)

    def test_role_existing_no_neutral_400(self):
        self.client.post(self.ws_list_url, {
            "source": self._video("a.mp4"),
            "preview": self._video("b.mp4"),
            "name": "pair608",
        })
        resp = self.client.post(
            "/api/v1/workspaces/pair608/video/preview/",
            {"file": self._video("c.mp4"), "existing": "source"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("existing", resp.json()["error"])

    def test_role_commit_invalid_400(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        with mock.patch.object(views.ws_fs, "commit_role_upload",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("плохая роль")):
            resp = self.client.post(
                f"/api/v1/workspaces/{self.ws_id}/video/preview/",
                {"file": self._video()})
        self.assertEqual(resp.status_code, 400)

    def test_role_commit_oserror_507(self):
        import logging
        import vc_pairs.views as views
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(views.ws_fs, "commit_role_upload",
                                   side_effect=OSError("disk")):
                resp = self.client.post(
                    f"/api/v1/workspaces/{self.ws_id}/video/preview/",
                    {"file": self._video()})
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)

    def test_role_write_oserror_507(self):
        import logging
        import vc_pairs.views as views
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch.object(views.ws_fs, "write_stream",
                                   side_effect=OSError("disk")):
                resp = self.client.post(
                    f"/api/v1/workspaces/{self.ws_id}/video/preview/",
                    {"file": self._video()})
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)

    def test_delete_role_not_explicit_400(self):
        hole = os.path.join(self._tmpdir, "rolehole")
        os.makedirs(hole, exist_ok=True)
        shutil.copy2(self.video, os.path.join(hole, "preview.mp4"))
        shutil.copy2(self.video, os.path.join(hole, "neut.mp4"))
        resp = self.client.delete("/api/v1/workspaces/rolehole/video/source/")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("не выделена", resp.json()["error"])

    def test_delete_role_closes_old_provider(self):
        import tempfile
        import vc_pairs.views as views
        from django.http import JsonResponse
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        tmp.write(b"x")
        tmp.close()
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        ws = mock.Mock()
        ws.original = "/tmp/other-source.mp4"
        ws.visualization = tmp.name
        ws.name = self.ws_id
        with mock.patch.object(views.ws_fs, "remove_role_file", return_value=tmp.name), \
             mock.patch.object(views.frame_provider, "close_source") as mclose, \
             mock.patch.object(views, "scan_workspaces"), \
             mock.patch.object(views, "bump_updated_at"), \
             mock.patch.object(views, "_entry_response", return_value=JsonResponse({"id": "x"})):
            resp = views._delete_role(ws, "preview")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(mclose.called)


class SwapCoverageTests(WorkspaceApiTestBase):
    def test_swap_missing_404(self):
        resp = self.client.post("/api/v1/workspaces/nonexistent/swap/")
        self.assertEqual(resp.status_code, 404)

    def test_swap_needs_two_400(self):
        resp = self.client.post(f"/api/v1/workspaces/{self.ws_id}/swap/")
        self.assertEqual(resp.status_code, 400)

    def test_swap_promotes_neutral_roles(self):
        import vc_pairs.views as views
        from django.http import JsonResponse
        ws = mock.Mock()
        ws.original = "/tmp/neut1.mp4"
        ws.visualization = "/tmp/neut2.mp4"
        ws.name = self.ws_id
        with mock.patch.object(views, "get_workspace", return_value=ws), \
             mock.patch.object(views.ws_fs, "promote_plain_video", return_value="/tmp/x") as mprom, \
             mock.patch.object(views.ws_fs, "swap_role_files",
                               return_value=("/tmp/s", "/tmp/p")), \
             mock.patch.object(views.frame_provider, "close_source"), \
             mock.patch.object(views, "scan_workspaces"), \
             mock.patch.object(views, "bump_updated_at"), \
             mock.patch.object(views, "_entry_response", return_value=JsonResponse({"id": "x"})):
            resp = self.client.post(f"/api/v1/workspaces/{self.ws_id}/swap/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mprom.call_count, 2)

    def test_swap_invalid_400(self):
        import vc_pairs.views as views
        from videocutter.standalone import workspace as ws_fs_mod
        ws = mock.Mock()
        ws.original = "/tmp/a.mp4"
        ws.visualization = "/tmp/b.mp4"
        ws.name = self.ws_id
        with mock.patch.object(views, "get_workspace", return_value=ws), \
             mock.patch.object(views.ws_fs, "promote_plain_video", return_value=None), \
             mock.patch.object(views.ws_fs, "swap_role_files",
                               side_effect=ws_fs_mod.InvalidWorkspaceError("нужны два файла")):
            resp = self.client.post(f"/api/v1/workspaces/{self.ws_id}/swap/")
        self.assertEqual(resp.status_code, 400)


class WorkspaceUnitCoverageTests(WorkspaceApiTestBase):
    def test_has_visualization(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        self.assertTrue(ws.has_visualization)
        empty_dir = os.path.join(self._tmpdir, "empty-hv")
        os.makedirs(empty_dir, exist_ok=True)
        empty = ws_module.Workspace(name="empty-hv", path=empty_dir)
        self.assertFalse(empty.has_visualization)

    def test_video_metadata_no_file(self):
        import workspace as ws_module
        empty_dir = os.path.join(self._tmpdir, "empty-vm")
        os.makedirs(empty_dir, exist_ok=True)
        ws = ws_module.Workspace(name="empty-vm", path=empty_dir)
        meta = ws.video_metadata("preview")
        self.assertEqual(meta["total_frames"], 0)

    def test_read_bad_position_kept_zero(self):
        import workspace as ws_module
        d = os.path.join(self._tmpdir, "badpos")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "fragments.tsv"), "w", encoding="utf-8") as f:
            f.write("# position\tnot-a-number\n")
            f.write("start\tend\tcomment\n0\t3\tx\n")
        ws = ws_module.Workspace(name="badpos", path=d)
        self.assertEqual(ws.load_position(), 0)

    def test_read_bad_settings_kept_default(self):
        import workspace as ws_module
        d = os.path.join(self._tmpdir, "badset")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "fragments.tsv"), "w", encoding="utf-8") as f:
            f.write("# settings\tbad\tbad\n")
            f.write("start\tend\tcomment\n0\t3\tx\n")
        ws = ws_module.Workspace(name="badset", path=d)
        q, s = ws.load_settings()
        self.assertEqual((q, s), (ws_module.DEFAULT_QUALITY, ws_module.DEFAULT_SCALE))

    def test_read_skips_broken_rows(self):
        import workspace as ws_module
        d = os.path.join(self._tmpdir, "badrows")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "fragments.tsv"), "w", encoding="utf-8") as f:
            f.write("start\tend\tcomment\n")
            f.write("bad\t3\tx\n")
            f.write("5\tbad\ty\n")
            f.write("0\t2\tok\n")
        ws = ws_module.Workspace(name="badrows", path=d)
        frags = ws.load_fragments()
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["comment"], "ok")

    def test_parse_csv_error_400(self):
        import csv as _csv
        import workspace as ws_module
        with mock.patch.object(ws_module.csv, "DictReader", side_effect=_csv.Error("bad")):
            frags, pos, problem = ws_module.parse_fragments_tsv_strict(
                b"start\tend\n0\t1\n", 10)
        self.assertIsNone(frags)
        self.assertEqual(problem[0], 400)

    def test_rename_workspace_error_moves(self):
        import workspace as ws_module
        ws_module.note_workspace_error("old-ws", "boom")
        ws_module.rename_workspace_error("old-ws", "new-ws")
        self.assertEqual(ws_module.workspace_error("new-ws"), "boom")
        self.assertIsNone(ws_module.workspace_error("old-ws"))
        ws_module.clear_workspace_error("new-ws")

    def test_updated_at_oserror_zero(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(ws_module.os.path, "getmtime", side_effect=OSError("gone")):
            self.assertEqual(ws_module._workspace_updated_at(ws), 0.0)

    def test_bump_updated_at_double_failure(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(ws_module.os, "utime", side_effect=OSError("ro")):
            ws_module.bump_updated_at(ws)

    def test_video_ver_oserror_fallback(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        _ = ws.original
        _ = ws.visualization
        with mock.patch.object(ws_module.os, "stat", side_effect=OSError("gone")):
            ver = ws_module._video_ver(ws)
        self.assertIn(".mp4", ver)

    def test_pair_warning_skipped(self):
        import workspace as ws_module
        w = ws_module._pair_warning(
            {"skipped_frames": 2, "total_frames": 10},
            {"skipped_frames": 3, "total_frames": 10}, True)
        self.assertIn("источнике", w)
        self.assertIn("превью", w)

    def test_pair_warning_mismatch(self):
        import workspace as ws_module
        w = ws_module._pair_warning(
            {"skipped_frames": 0, "total_frames": 10},
            {"skipped_frames": 0, "total_frames": 9}, True)
        self.assertIn("не совпадает", w)

    def test_task_dates_oserror_none(self):
        import workspace as ws_module
        import task_meta as tm
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(tm, "load", side_effect=OSError("gone")):
            self.assertEqual(ws_module._task_dates(ws), (None, None, None))

    def test_task_display_oserror_fallback(self):
        import workspace as ws_module
        import task_meta as tm
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(tm, "load", side_effect=OSError("gone")):
            self.assertEqual(ws_module._task_display_name(ws), ws.name)

    def test_export_state_import_failure_none(self):
        import sys as _sys
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.dict(_sys.modules, {"vc_fragments.views": None}):
            self.assertIsNone(ws_module._export_state(ws.name))

    def test_fast_role_meta_no_path(self):
        import workspace as ws_module
        empty_dir = os.path.join(self._tmpdir, "empty-fast")
        os.makedirs(empty_dir, exist_ok=True)
        ws = ws_module.Workspace(name="empty-fast", path=empty_dir)
        meta, ready = ws_module._fast_role_meta(ws, "preview")
        self.assertTrue(ready)
        self.assertEqual(meta["total_frames"], 0)

    def test_fast_role_meta_index_error_raises(self):
        import workspace as ws_module
        from vc_pairs import frame_provider as fp
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(fp, "try_get_metadata", return_value=None), \
             mock.patch.object(fp, "index_error", return_value="boom"):
            with self.assertRaises(ValueError):
                ws_module._fast_role_meta(ws, "preview")

    def test_status_broken_via_error(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        ws_module.note_workspace_error(self.ws_id, "boom")
        try:
            st = ws_module.workspace_status_entry(ws)
            self.assertTrue(st["broken"])
        finally:
            ws_module.clear_workspace_error(self.ws_id)

    def test_status_index_error_broken(self):
        import workspace as ws_module
        ws = ws_module.get_workspace(self.ws_id)
        with mock.patch.object(ws_module, "_index_state", side_effect=ValueError("idx fail")):
            st = ws_module.workspace_status_entry(ws)
        self.assertTrue(st["broken"])

    def test_detail_slow_broken(self):
        import workspace as ws_module
        with mock.patch.object(ws_module, "_pair_entry",
                               side_effect=[{"indexing": False}, ValueError("boom")]):
            detail = ws_module.get_workspace_detail(self.ws_id)
        self.assertTrue(detail["broken"])

    def test_safe_returns_default(self):
        import workspace as ws_module

        def _boom():
            raise RuntimeError("x")
        self.assertEqual(ws_module._safe(_boom, "dflt"), "dflt")


class TaskMetaCoverageTests(WorkspaceApiTestBase):
    def test_as_str_list_non_list(self):
        import task_meta as tm
        out = tm.validate({"assignees": "ivan"})
        self.assertEqual(out["assignees"], [])

    def test_as_str_list_filters(self):
        import task_meta as tm
        out = tm.validate({"assignees": ["ivan", 42, None, ["x"]]})
        self.assertEqual(out["assignees"], ["ivan", "42"])

    def test_notes_none_becomes_empty(self):
        import task_meta as tm
        out = tm.validate({"notes": None})
        self.assertEqual(out["notes"], "")

    def test_name_none_becomes_empty(self):
        import task_meta as tm
        out = tm.validate({"name": None})
        self.assertEqual(out["name"], "")

    def test_name_non_string_rejected(self):
        import task_meta as tm
        with self.assertRaises(ValueError):
            tm.validate({"name": 42})

    def test_project_id_non_string_rejected(self):
        import task_meta as tm
        with self.assertRaises(ValueError):
            tm.validate({"project_id": 42})

    def test_project_id_blank_becomes_none(self):
        import task_meta as tm
        out = tm.validate({"project_id": "   "})
        self.assertIsNone(out["project_id"])

    def test_now_iso_ok(self):
        import task_meta as tm
        self.assertTrue(tm.now_iso())
        self.assertTrue(tm.init_new()["created_at"])
