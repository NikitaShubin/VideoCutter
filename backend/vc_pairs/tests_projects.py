# -*- coding: utf-8 -*-
"""Тесты проектов: миграция в default, CRUD, привязка задач, удаление.

Прогон: cd backend && python manage.py test

Модель файловая (project.json + project_id в task.json), без БД —
проверяется HTTP-слой и записи на диске (см. docs/project-model.md).
"""

import json
import os
from urllib.parse import quote

import task_meta
from vc_fragments.tests import WorkspaceApiTestBase

HTTP_CREATED = 201
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409


def u(text: str) -> str:
    """Компонент URL (проекты носят имя-в-id, кириллица кодируется)."""
    return quote(text, safe="")


class ProjectTestBase(WorkspaceApiTestBase):
    """База: URL-ы проектов + JSON-POST/PUT-хелперы."""

    def setUp(self):
        super().setUp()
        self.projects_url = "/api/v1/projects/"
        self.default_url = "/api/v1/projects/default/"

    def json_post(self, url, payload):
        return self.client.post(
            url,
            data=json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )

    def project_url(self, pid: str) -> str:
        return f"/api/v1/projects/{u(pid)}/"

    def tasks_url(self, pid: str) -> str:
        return f"/api/v1/projects/{u(pid)}/tasks/"

    def create_project(self, name) -> dict:
        resp = self.json_post(self.projects_url, {"name": name})
        self.assertEqual(resp.status_code, HTTP_CREATED, resp.content)
        return resp.json()


class MigrationTests(ProjectTestBase):
    def test_first_scan_moves_tasks_to_default(self):
        """Старые задачи без привязки молча попадают в проект default."""
        resp = self.client.get(self.ws_list_url)  # первый скан → миграция
        self.assertEqual(resp.status_code, 200)

        projects = self.client.get(self.projects_url).json()
        ids = [p["id"] for p in projects]
        self.assertIn("default", ids)

        meta = task_meta.load(self.ws_dir)
        self.assertEqual(meta["project_id"], "default")

        item = next(w for w in resp.json() if w["id"] == self.ws_id)
        self.assertEqual(item["project_id"], "default")

        marker = os.path.join(self._tmpdir, ".projects_migrated")
        self.assertTrue(os.path.isfile(marker))

    def test_marker_makes_migration_one_shot(self):
        """После миграции новый standalone не перевешивается обратно."""
        self.client.get(self.ws_list_url)
        # Явный detach = осознанный standalone.
        resp = self.client.delete(
            f"/api/v1/projects/default/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(task_meta.load(self.ws_dir)["project_id"])

        self.client.get(self.ws_list_url)  # повторные сканы
        self.client.get(self.ws_list_url)
        self.assertIsNone(task_meta.load(self.ws_dir)["project_id"])

    def test_projects_dir_not_listed_as_task(self):
        """Папка реестра projects/ — не задача в списке."""
        self.create_project("secret")
        ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertNotIn("projects", ids)

    def test_upload_rejects_reserved_name(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        src = os.path.join(os.path.dirname(__file__), "..", "testdata",
                           "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        video = SimpleUploadedFile("x.mp4", data, content_type="video/mp4")
        resp = self.client.post(self.ws_list_url, {"file": video,
                                                   "name": "projects"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("зарезервировано", resp.json()["error"])


class ProjectCrudTests(ProjectTestBase):
    def test_create_and_list(self):
        p = self.create_project("Входженко")
        self.assertEqual(p["id"], "Входженко")
        self.assertEqual(p["name"], "Входженко")
        self.assertEqual(p["task_count"], 0)
        self.assertTrue(p["created_at"])
        self.assertIsInstance(p["model"], dict)

        items = self.client.get(self.projects_url).json()
        # GET проектов гоняет первый скан → миграция создаёт default.
        self.assertEqual([i["id"] for i in items], ["default", "Входженко"])

    def test_create_duplicate_409(self):
        self.create_project("dup")
        resp = self.json_post(self.projects_url, {"name": "dup"})
        self.assertEqual(resp.status_code, HTTP_CONFLICT)

    def test_create_requires_name(self):
        resp = self.json_post(self.projects_url, {})
        self.assertEqual(resp.status_code, 400)

    def test_create_rejects_path_name(self):
        resp = self.json_post(self.projects_url, {"name": "../evil"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self._tmpdir), "evil")))

    def test_detail_404(self):
        resp = self.client.get("/api/v1/projects/nope/")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_rename(self):
        self.create_project("old")
        resp = self.json_put("/api/v1/projects/old/", {"name": "new"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["name"], "new")
        # id неизменяем — это имя папки.
        self.assertEqual(resp.json()["id"], "old")
        self.assertTrue(os.path.isfile(
            os.path.join(self._tmpdir, "projects", "old", "project.json")))

    def test_rename_rejects_unknown_fields(self):
        self.create_project("f")
        resp = self.json_put("/api/v1/projects/f/", {"bogus": 1})
        self.assertEqual(resp.status_code, 400)

    def test_update_model_stub(self):
        self.create_project("m")
        resp = self.json_put("/api/v1/projects/m/", {
            "model": {"id": None, "kind": "sam2", "params": {"iou": 0.8}},
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["model"]["kind"], "sam2")


class AttachTests(ProjectTestBase):
    def setUp(self):
        super().setUp()
        self.client.get(self.ws_list_url)  # миграция: test-ws → default
        self.p = self.create_project("Аннотация")

    def attach(self, pid, task_id):
        return self.json_post(self.tasks_url(pid), {"task_id": task_id})

    def test_attach_moves_between_projects(self):
        resp = self.attach(self.p["id"], self.ws_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["project_id"], "Аннотация")
        self.assertEqual(task_meta.load(self.ws_dir)["project_id"],
                         "Аннотация")

        detail = self.client.get(self.ws_detail_url).json()
        self.assertEqual(detail["project_id"], "Аннотация")

        counts = {p["id"]: p["task_count"]
                  for p in self.client.get(self.projects_url).json()}
        self.assertEqual(counts["Аннотация"], 1)
        self.assertEqual(counts["default"], 0)

    def test_attach_missing_task_404(self):
        resp = self.attach(self.p["id"], "no-such-task")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_attach_missing_project_404(self):
        resp = self.attach("nope", self.ws_id)
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_attach_reserved_task_name_404(self):
        resp = self.attach(self.p["id"], "projects")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_detach(self):
        resp = self.client.delete(
            f"/api/v1/projects/default/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(task_meta.load(self.ws_dir)["project_id"])
        # Повторная отвязка — 404 (задача уже не в проекте).
        resp = self.client.delete(
            f"/api/v1/projects/default/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_membership_does_not_reorder_recent(self):
        """Перевеска не двигает mtime папки — «Недавние» не прыгают."""
        dir_mtime = os.path.getmtime(self.ws_dir)
        resp = self.attach(self.p["id"], self.ws_id)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(os.path.getmtime(self.ws_dir), dir_mtime)


class ListFilterTests(ProjectTestBase):
    def test_filter_by_project(self):
        self.client.get(self.ws_list_url)  # миграция → default
        other = self.create_project("Второй")
        self.json_post(self.tasks_url(other["id"]),
                       {"task_id": self.ws_id})

        default_only = self.client.get(
            f"{self.ws_list_url}?project=default").json()
        self.assertEqual([w["id"] for w in default_only], [])

        other_only = self.client.get(
            f"{self.ws_list_url}?project={u('Второй')}").json()
        self.assertEqual([w["id"] for w in other_only], [self.ws_id])

        none_only = self.client.get(f"{self.ws_list_url}?project=none").json()
        self.assertEqual([w["id"] for w in none_only], [])

        everything = self.client.get(self.ws_list_url).json()
        self.assertEqual(len(everything), 1)

    def test_filter_none_shows_standalone(self):
        self.client.get(self.ws_list_url)
        self.client.delete(
            f"/api/v1/projects/default/tasks/{self.ws_id}/")
        none_only = self.client.get(f"{self.ws_list_url}?project=none").json()
        self.assertEqual([w["id"] for w in none_only], [self.ws_id])


class ProjectDeleteTests(ProjectTestBase):
    def setUp(self):
        super().setUp()
        self.client.get(self.ws_list_url)  # миграция → default

    def test_delete_keep_tasks(self):
        resp = self.client.delete(f"{self.default_url}?with_tasks=keep")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["tasks"], {"kept": 1})
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, "projects", "default")))
        # Задача выжила и стала standalone.
        self.assertTrue(os.path.isdir(self.ws_dir))
        self.assertIsNone(task_meta.load(self.ws_dir)["project_id"])

    def test_delete_cascade_removes_tasks(self):
        resp = self.client.delete(self.default_url)  # дефолт = cascade
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["tasks"], {"deleted": 1})
        self.assertFalse(os.path.isdir(self.ws_dir))
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, "projects", "default")))
        self.assertEqual(self.client.get(self.ws_list_url).json(), [])

    def test_delete_move_tasks(self):
        target = self.create_project("Приёмник")
        resp = self.client.delete(
            f"{self.default_url}?with_tasks=move&to={u(target['id'])}")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["tasks"], {"moved": 1, "to": "Приёмник"})
        self.assertEqual(task_meta.load(self.ws_dir)["project_id"],
                         "Приёмник")

    def test_delete_move_requires_target(self):
        resp = self.client.delete(f"{self.default_url}?with_tasks=move")
        self.assertEqual(resp.status_code, 400)

    def test_delete_invalid_mode(self):
        resp = self.client.delete(f"{self.default_url}?with_tasks=whatever")
        self.assertEqual(resp.status_code, 400)

    def test_delete_missing_404(self):
        resp = self.client.delete("/api/v1/projects/nope/")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)


class UploadJoinsDefaultTests(ProjectTestBase):
    def test_new_task_joins_default_when_exists(self):
        """Свежая установка: пока default существует, новые задачи в него."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.create_project("default")  # имитация: реестр уже есть
        src = os.path.join(os.path.dirname(__file__), "..", "testdata",
                           "test.mp4")
        with open(src, "rb") as f:
            data = f.read()
        video = SimpleUploadedFile("n.mp4", data, content_type="video/mp4")
        resp = self.client.post(self.ws_list_url,
                                {"file": video, "name": "fresh"})
        self.assertEqual(resp.status_code, 202, resp.content)

        deadline = __import__("time").time() + 30
        while True:
            body = self.client.get("/api/v1/workspaces/fresh/").json()
            if not body.get("indexing"):
                break
            if __import__("time").time() > deadline:
                self.fail("background validation never finished")
            __import__("time").sleep(0.1)
        self.assertFalse(body.get("broken"), body.get("error"))
        self.assertEqual(body["project_id"], "default")
