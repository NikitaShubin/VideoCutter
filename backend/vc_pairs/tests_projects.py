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


# ─── Добивка покрытия vc_pairs/projects.py и project_meta.py ─────────────────
# Новые классы ниже закрывают ветки, не задетые основными тестами: битый JSON,
# пустые/чужие тела, ошибки сохранения и удаления (5xx), краевые случаи
# project_meta (битые project.json, мусор в реестре, сбои ФС при миграции).

import logging
from unittest import mock

import project_meta


class ProjectLoadJsonTests(ProjectTestBase):
    """Битые и не-объектные тела запросов (vc_pairs/projects.py: _load_json)."""

    def test_post_broken_json_400(self):
        resp = self.client.post(
            self.projects_url, data="{bad",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_post_null_body_400(self):
        resp = self.client.post(
            self.projects_url, data="null",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_post_list_body_400(self):
        resp = self.client.post(
            self.projects_url, data="[1, 2]",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_attach_broken_json_400(self):
        p = self.create_project("Привязка")
        resp = self.client.post(
            self.tasks_url(p["id"]), data="{bad",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_attach_missing_task_id_400(self):
        p = self.create_project("Привязка")
        for bad in ({}, {"task_id": "  "}, {"task_id": 5}):
            resp = self.json_post(self.tasks_url(p["id"]), bad)
            self.assertEqual(resp.status_code, 400, bad)


class ProjectDetailUpdateTests(ProjectTestBase):
    """GET детали и ветки PUT (projects.py: 107, 115, 118, 127, 132, 136-137)."""

    def test_detail_ok(self):
        p = self.create_project("Детали")
        resp = self.client.get(self.project_url(p["id"]))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["id"], p["id"])
        self.assertEqual(resp.json()["name"], "Детали")

    def test_put_missing_project_404(self):
        resp = self.json_put("/api/v1/projects/nope/", {"name": "x"})
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_put_broken_json_400(self):
        p = self.create_project("Правка")
        resp = self.client.put(
            self.project_url(p["id"]), data="{bad",
            content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_put_empty_name_400(self):
        p = self.create_project("Правка")
        resp = self.json_put(self.project_url(p["id"]), {"name": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_put_model_not_dict_400(self):
        p = self.create_project("Правка")
        resp = self.json_put(self.project_url(p["id"]), {"model": "sam2"})
        self.assertEqual(resp.status_code, 400)

    def test_put_save_error_400(self):
        p = self.create_project("Правка")
        with mock.patch.object(
                project_meta, "save", side_effect=ValueError("плохая модель")):
            resp = self.json_put(
                self.project_url(p["id"]), {"name": "ok"})
        self.assertEqual(resp.status_code, 400)


class ProjectDeleteDetachEdgeTests(ProjectTestBase):
    """Редкие ветки DELETE/DETACH (projects.py: 154, 167-170, 182-184, 219-223,
    231-232)."""

    def test_delete_move_unknown_target_404(self):
        self.client.get(self.ws_list_url)  # миграция → default
        resp = self.client.delete(
            f"{self.default_url}?with_tasks=move&to=nope")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_delete_cascade_failure_500(self):
        from django.http import JsonResponse

        self.client.get(self.ws_list_url)  # миграция: test-ws → default
        with mock.patch(
                "vc_pairs.projects._workspace_delete",
                return_value=JsonResponse({"error": "boom"}, status=500)):
            logging.disable(logging.CRITICAL)
            try:
                resp = self.client.delete(self.default_url)
            finally:
                logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 500)
        # Прерванный каскад оставляет проект и задачу на месте.
        self.assertTrue(project_meta.exists("default"))
        self.assertTrue(os.path.isdir(self.ws_dir))

    def test_delete_project_rm_failure_500(self):
        self.client.get(self.ws_list_url)
        with mock.patch.object(
                project_meta, "delete", side_effect=OSError("диск занят")):
            logging.disable(logging.CRITICAL)
            try:
                resp = self.client.delete(
                    f"{self.default_url}?with_tasks=keep")
            finally:
                logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 500)

    def test_detach_missing_project_404(self):
        resp = self.client.delete(
            f"/api/v1/projects/nope/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, HTTP_NOT_FOUND)

    def test_detach_bad_task_name_400(self):
        self.create_project("Целевой")
        resp = self.client.delete(
            f"/api/v1/projects/{u('Целевой')}/tasks/{u('bad\\name')}/")
        self.assertEqual(resp.status_code, 400)

    def test_detach_backend_error_400(self):
        self.client.get(self.ws_list_url)  # миграция: test-ws → default
        with mock.patch.object(
                project_meta, "detach", side_effect=ValueError("сбой")):
            resp = self.client.delete(
                f"/api/v1/projects/default/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, 400)


class ProjectMetaValidateTests(ProjectTestBase):
    """Ветки validate (project_meta.py: 90, 93, 96, 100)."""

    def test_validate_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            project_meta.validate({"name": 123})

    def test_validate_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            project_meta.validate({"name": "x", "id": "   "})

    def test_validate_rejects_bad_model(self):
        with self.assertRaises(ValueError):
            project_meta.validate({"name": "x", "model": "sam2"})

    def test_validate_none_model_gets_default(self):
        out = project_meta.validate({"name": "x", "model": None})
        self.assertEqual(
            out["model"], {"id": None, "kind": "", "params": {}})


class ProjectMetaLoadTests(ProjectTestBase):
    """Ветки exists/load/list (project_meta.py: 108-109, 117-118, 122-134,
    172, 180, 184, 186, 189)."""

    def _write_project_file(self, pid, text):
        d = os.path.join(self._tmpdir, "projects", pid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "project.json"), "w",
                  encoding="utf-8") as f:
            f.write(text)

    def test_exists_bad_pid_false(self):
        self.assertFalse(project_meta.exists("../evil"))

    def test_load_bad_pid_none(self):
        self.assertIsNone(project_meta.load("../evil"))

    def test_load_missing_none(self):
        self.assertIsNone(project_meta.load("ghost-xyz"))

    def test_load_broken_json_defaults(self):
        self._write_project_file("broken", "{not json")
        self.assertEqual(
            project_meta.load("broken"), project_meta.defaults())

    def test_load_non_dict_defaults(self):
        self._write_project_file("nondict", "[1, 2]")
        self.assertEqual(
            project_meta.load("nondict"), project_meta.defaults())

    def test_load_invalid_dict_defaults(self):
        self._write_project_file("invalid", '{"name": 123}')
        self.assertEqual(
            project_meta.load("invalid"), project_meta.defaults())

    def test_delete_missing_raises(self):
        with self.assertRaises(ValueError):
            project_meta.delete("ghost-proj")

    def test_list_empty_root(self):
        self.assertEqual(
            project_meta.list_projects(
                root=os.path.join(self._tmpdir, "no-such")), [])

    def test_list_skips_junk(self):
        self.create_project("Настоящий")
        base = os.path.join(self._tmpdir, "projects")
        os.makedirs(os.path.join(base, ".hidden"), exist_ok=True)
        with open(os.path.join(base, "junk.txt"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(base, "empty-no-json"), exist_ok=True)
        ids = [m["id"] for m in project_meta.list_projects()]
        self.assertEqual(ids, ["Настоящий"])


class ProjectMetaMembershipTests(ProjectTestBase):
    """Членство и его ФС-края (project_meta.py: 203, 230-231, 237, 250-251,
    256-257, 264, 275, 278, 292, 294)."""

    def test_task_project_ids_missing_root(self):
        self.assertEqual(
            project_meta.task_project_ids(
                root=os.path.join(self._tmpdir, "no-such")), {})

    def test_can_attach_bad_task_name(self):
        err = project_meta.can_attach("../evil", "default")
        self.assertIn("не найдена", err)

    def test_can_attach_missing_project(self):
        err = project_meta.can_attach(self.ws_id, "ghost-proj")
        self.assertIn("не найден", err)

    def test_attach_missing_task_raises(self):
        with self.assertRaises(ValueError):
            project_meta.attach("ghost-task", "default")

    def test_detach_missing_task_raises(self):
        with self.assertRaises(ValueError):
            project_meta.detach("ghost-task")

    def test_detach_standalone_noop(self):
        # У задачи из фикстуры нет task.json — отвязка молча ничего не делает.
        self.assertIsNone(project_meta.detach(self.ws_id))

    def test_set_project_for_all_noop(self):
        self.create_project("default")
        project_meta.attach(self.ws_id, "default")
        self.assertEqual(project_meta.set_project_for_all("default"), 0)

    def test_set_project_for_all_skips_bound_when_only_null(self):
        self.create_project("default")
        project_meta.attach(self.ws_id, "default")
        self.assertEqual(
            project_meta.set_project_for_all("other", only_null=True), 0)

    def test_save_membership_stat_failure(self):
        meta = task_meta.load(self.ws_dir)
        with mock.patch.object(
                project_meta.os, "stat", side_effect=OSError("boom")):
            project_meta._save_membership(self.ws_dir, meta)
        self.assertTrue(
            os.path.isfile(os.path.join(self.ws_dir, "task.json")))

    def test_save_membership_utime_failure(self):
        meta = task_meta.load(self.ws_dir)
        with mock.patch.object(
                project_meta.os, "utime", side_effect=OSError("ro")):
            project_meta._save_membership(self.ws_dir, meta)
        self.assertTrue(
            os.path.isfile(os.path.join(self.ws_dir, "task.json")))


class ProjectMetaMigrationTests(ProjectTestBase):
    """Разовая миграция: края ФС (project_meta.py: 315-316, 322, 331-332,
    344-345)."""

    def test_ensure_migrated_missing_root(self):
        project_meta._ensure_migrated_impl(
            root=os.path.join(self._tmpdir, "no-such-root"))

    def test_ensure_migrated_swallows_errors(self):
        with mock.patch.object(
                project_meta, "task_project_ids",
                side_effect=RuntimeError("сбой скана")):
            project_meta.ensure_migrated(root=self._tmpdir)

    def test_ensure_migrated_create_race(self):
        from videocutter.standalone import workspace as ws_fs

        with mock.patch.object(
                project_meta, "create",
                side_effect=ws_fs.WorkspaceExistsError("уже есть")):
            project_meta._ensure_migrated_impl(root=self._tmpdir)
        self.assertFalse(project_meta.exists("default"))

    def test_ensure_migrated_marker_write_failure(self):
        marker = os.path.join(self._tmpdir, project_meta.MIGRATION_MARKER)
        os.makedirs(marker + ".tmp", exist_ok=True)  # open(tmp) → IsADirectory
        project_meta._ensure_migrated_impl(root=self._tmpdir)
        self.assertFalse(os.path.isfile(marker))
