# -*- coding: utf-8 -*-
"""P3: бэкап/восстановление задач и проектов, экспорт/импорт разметки.

Прогон: cd backend && python manage.py test

Два слоя. Юнит-тесты чистых модулей: ``backup_format`` (манифест, версии,
безопасность имён, отпечаток) и ``workspace.parse_fragments_tsv_strict``
(строгий парсер) — без контура. HTTP-слой целиком: roundtrip «скачал →
залил», все режимы on_conflict, отказы (zip-slip, чужой манифест, чужой
отпечаток, ENOSPC без мусора на диске), стейджинг со свипом, членство
задач (override / из архива / standalone, join_default=False).

Правила формата и сценарии — docs/backup-model.md.
"""

import errno
import io
import json
import logging
import os
import time
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

import backup_format as fmt
import project_meta
import task_meta
import workspace
from vc_fragments.tests import WorkspaceApiTestBase
from vc_pairs import frame_provider
from vc_pairs.backup import _task_members
from vc_pairs.tests_projects import ProjectTestBase, u
from vc_pairs.tests_workspace import _wait_ready
from workspace import parse_fragments_tsv_strict

#: Разметка, совпадающая с фикстурой WorkspaceApiTestBase (2 фрагмента).
TSV_FIXTURE = "start\tend\tcomment\n0\t3\tстарт\n5\t9\tфиниш\n".encode("utf-8")


def build_zip(members) -> bytes:
    """Собирает zip в памяти: {имя члена: байты}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def fetch_zip(resp) -> bytes:
    """Вычитывает FileResponse и закрывает его — временный файл удалён."""
    data = b"".join(resp.streaming_content)
    resp.close()
    return data


def manifest_json(**changes) -> bytes:
    """manifest.json бэкапа задачи (kind=backup) с накладкой изменений."""
    manifest = fmt.make_manifest(fmt.KIND_BACKUP, resource=fmt.RESOURCE_TASK)
    manifest.update(changes)
    return json.dumps(manifest, ensure_ascii=False).encode("utf-8")


def annotations_manifest_json(**changes) -> bytes:
    """manifest.json разметки (kind=annotations) с накладкой изменений."""
    manifest = fmt.make_manifest(
        fmt.KIND_ANNOTATIONS, resource=fmt.RESOURCE_TASK)
    manifest.update(changes)
    return json.dumps(manifest, ensure_ascii=False).encode("utf-8")


def rewrite_manifest(data: bytes, mutate) -> bytes:
    """Пересобирает архив с манифестом, изменённым mutate(dict)."""
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        members = {name: src.read(name) for name in src.namelist()}
    manifest = json.loads(members[fmt.MANIFEST_NAME].decode("utf-8"))
    mutate(manifest)
    members[fmt.MANIFEST_NAME] = json.dumps(
        manifest, ensure_ascii=False).encode("utf-8")
    return build_zip(members)


class BackupArchiveMixin:
    """Хелперы P3: скачать архив (streaming) и залить POST-импорт."""

    def fetch_zip_url(self, url: str) -> bytes:
        resp = self.client.get(url)
        if resp.status_code != 200:  # не-200 — JsonResponse, content читаем
            self.fail(f"GET {url} → {resp.status_code}: {resp.content!r}")
        return fetch_zip(resp)

    def import_archive(self, data: bytes, query: str = "",
                       path: str = "/api/v1/backups/import"):
        return self.client.post(
            f"{path}{query}",
            {"file": SimpleUploadedFile(
                "backup.zip", data, content_type="application/zip")})


def _wait_validation(testcase, ws_id: str, path: str) -> dict:
    """Ждёт конца фоновой валидации: паспорт дописан (created_at).

    ``created_at`` штампует только валидация — её absence значит, что тред
    ещё работает (или упал), и членство может дозаписаться позже.
    """
    _wait_ready(testcase, ws_id)
    deadline = time.time() + 30
    while True:
        meta = task_meta.load(path)
        if meta.get("created_at"):
            return meta
        if time.time() > deadline:
            testcase.fail(f"паспорт {ws_id} не получил created_at")
        time.sleep(0.1)


# ─── Юнит: формат архива ─────────────────────────────────────────────────────

class BackupFormatTests(SimpleTestCase):
    """Чистый модуль backup_format: манифест, версии, имена, отпечаток."""

    def test_make_manifest_service_fields(self):
        manifest = fmt.make_manifest(
            fmt.KIND_BACKUP, resource=fmt.RESOURCE_TASK,
            task_id="t1", format="evil")
        # Служебные поля перетереть нельзя.
        self.assertEqual(manifest["format"], fmt.FORMAT_NAME)
        self.assertEqual(manifest["format_version"], fmt.FORMAT_VERSION)
        self.assertEqual(manifest["kind"], fmt.KIND_BACKUP)
        self.assertEqual(manifest["resource"], fmt.RESOURCE_TASK)
        self.assertEqual(manifest["task_id"], "t1")
        self.assertTrue(manifest["created_at"])

    def test_make_manifest_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            fmt.make_manifest("cvat")

    def test_manifest_problem_accepts_our_manifest(self):
        self.assertIsNone(fmt.manifest_problem(fmt.make_manifest(fmt.KIND_BACKUP)))

    def test_manifest_problem_foreign_format(self):
        manifest = fmt.make_manifest(fmt.KIND_BACKUP)
        manifest["format"] = "cvat"
        self.assertIn("не архив VideoCutter", fmt.manifest_problem(manifest))

    def test_manifest_problem_version_gate(self):
        manifest = fmt.make_manifest(fmt.KIND_BACKUP)
        manifest["format_version"] = "2.0"
        self.assertIn("не читается", fmt.manifest_problem(manifest))
        # Младшая minor-версия нашего major читается.
        manifest["format_version"] = "1.9"
        self.assertIsNone(fmt.manifest_problem(manifest))
        manifest["format_version"] = "нечисло"
        self.assertIn("некорректная версия", fmt.manifest_problem(manifest))

    def test_manifest_problem_unknown_kind_and_resource(self):
        manifest = fmt.make_manifest(fmt.KIND_BACKUP)
        manifest["kind"] = "cvat-task"
        self.assertIn("kind", fmt.manifest_problem(manifest))
        manifest = fmt.make_manifest(fmt.KIND_BACKUP)
        manifest["resource"] = "folder"
        self.assertIn("resource", fmt.manifest_problem(manifest))

    def test_manifest_problem_field_types(self):
        # task_id/project_id обязаны быть строками (или null).
        msg = fmt.manifest_problem(fmt.make_manifest(fmt.KIND_BACKUP, task_id=42))
        self.assertIn("task_id", msg)
        self.assertIsNotNone(fmt.manifest_problem([1, 2]))

    def test_manifest_problem_ignores_unknown_fields(self):
        manifest = fmt.make_manifest(fmt.KIND_BACKUP, future_field={"x": 1})
        self.assertIsNone(fmt.manifest_problem(manifest))

    def test_member_problem_blocks_escapes(self):
        for name in ("../evil", "a/../../evil", "/etc/passwd", "C:/win",
                     "..", "back\\slash", "nul\x00.txt", "tab\t.txt", ""):
            with self.subTest(name=name):
                self.assertIsNotNone(fmt.member_problem(name))

    def test_member_problem_allows_normal_names(self):
        for name in ("manifest.json", "task_0/visualization.mp4", "dir/",
                     "разметка/файл.tsv"):
            with self.subTest(name=name):
                self.assertIsNone(fmt.member_problem(name))

    def test_open_archive_rejects_garbage(self):
        with self.assertRaises(fmt.ArchiveError) as ctx:
            fmt.open_archive(io.BytesIO(b"not-a-zip-at-all"))
        self.assertIn("zip", str(ctx.exception))

    def test_open_archive_requires_manifest(self):
        with self.assertRaises(fmt.ArchiveError) as ctx:
            fmt.open_archive(io.BytesIO(build_zip({"video.mp4": b"x"})))
        self.assertIn("нет manifest.json", str(ctx.exception))

    def test_open_archive_rejects_broken_manifest(self):
        with self.assertRaises(fmt.ArchiveError) as ctx:
            fmt.open_archive(io.BytesIO(
                build_zip({"manifest.json": b"{broken"})))
        self.assertIn("не читается", str(ctx.exception))
        # JSON есть, но не объект.
        with self.assertRaises(fmt.ArchiveError):
            fmt.open_archive(io.BytesIO(
                build_zip({"manifest.json": b"[1, 2]"})))

    def test_open_archive_rejects_traversal_member(self):
        data = build_zip({"manifest.json": manifest_json(),
                          "../evil.txt": b"pwn"})
        with self.assertRaises(fmt.ArchiveError) as ctx:
            fmt.open_archive(io.BytesIO(data))
        self.assertIn("Небезопасное", str(ctx.exception))

    def test_open_archive_returns_members_and_manifest(self):
        data = build_zip({"manifest.json": manifest_json(task_id="t1")})
        zf, names, manifest = fmt.open_archive(io.BytesIO(data))
        try:
            self.assertEqual(names, ["manifest.json"])
            self.assertEqual(manifest["task_id"], "t1")
        finally:
            zf.close()

    def test_read_json_member_rules(self):
        data = build_zip({
            "manifest.json": manifest_json(),
            "task.json": json.dumps({"status": "completed"}).encode("utf-8"),
            "note.txt": b"not json",
            "list.json": b"[1, 2]",
        })
        zf, _, _ = fmt.open_archive(io.BytesIO(data))
        try:
            meta = fmt.read_json_member(zf, "task.json")
            self.assertEqual(meta["status"], "completed")
            for member in ("absent.json", "note.txt", "list.json"):
                with self.subTest(member=member):
                    with self.assertRaises(fmt.ArchiveError):
                        fmt.read_json_member(zf, member)
        finally:
            zf.close()

    def test_fingerprint_problem_rules(self):
        want = {"total_frames": 10, "width": 640}
        self.assertIsNone(fmt.fingerprint_problem(want, {"total_frames": 10}))
        msg = fmt.fingerprint_problem(want, {"total_frames": 9})
        self.assertIn("другого видео", msg)
        # Нулевые значения (индекс не готов / нет в архиве) — сверка off.
        self.assertIsNone(fmt.fingerprint_problem(None, {"total_frames": 10}))
        self.assertIsNone(fmt.fingerprint_problem(
            {"total_frames": 0}, {"total_frames": 10}))
        self.assertIsNone(fmt.fingerprint_problem(want, {"total_frames": 0}))
        self.assertIsNone(fmt.fingerprint_problem(
            {"total_frames": "много"}, {"total_frames": 10}))

    def test_fragment_filename_matches_workspace(self):
        self.assertEqual(fmt.FRAGMENTS_FILE, workspace.FRAGMENTS_FILE)


# ─── Юнит: разбор членов задачи по префиксу ──────────────────────────────────

class TaskMembersTests(SimpleTestCase):
    """_task_members: чужие префиксы отбрасываются, а не переинтерпретируются.

    Регрессия: слепой срез ``member[len(prefix):]`` превращал
    ``task_0/task.json`` под префиксом ``task_1/`` в ``task.json`` — вторая
    задача проекта забирала паспорт и разметку первой.
    """

    NAMES = ["manifest.json", "project.json",
             "task_0/task.json", "task_0/fragments.tsv",
             "task_0/visualization.mp4",
             "task_1/task.json", "task_1/fragments.tsv",
             "task_1/clip.mp4"]

    def test_prefix_filters_foreign_members(self):
        videos, passport, tsv = _task_members(self.NAMES, "task_1/")
        self.assertEqual(videos, ["task_1/clip.mp4"])
        self.assertEqual(passport, "task_1/task.json")
        self.assertEqual(tsv, "task_1/fragments.tsv")

    def test_first_prefix_unaffected(self):
        videos, passport, tsv = _task_members(self.NAMES, "task_0/")
        self.assertEqual(videos, ["task_0/visualization.mp4"])
        self.assertEqual(passport, "task_0/task.json")
        self.assertEqual(tsv, "task_0/fragments.tsv")

    def test_empty_prefix_task_backup(self):
        names = ["manifest.json", "task.json", "fragments.tsv",
                 "visualization.mp4"]
        videos, passport, tsv = _task_members(names, "")
        self.assertEqual(videos, ["visualization.mp4"])
        self.assertEqual(passport, "task.json")
        self.assertEqual(tsv, "fragments.tsv")


# ─── Юнит: строгий парсер разметки ───────────────────────────────────────────

class StrictParserTests(SimpleTestCase):
    """parse_fragments_tsv_strict: импорт не теряет данные молча."""

    def parse(self, data: bytes, total: int = 10):
        return parse_fragments_tsv_strict(data, total)

    def test_valid_rows_preserve_comments(self):
        frags, position, problem = self.parse(TSV_FIXTURE)
        self.assertIsNone(problem)
        self.assertEqual(position, 0)
        self.assertEqual(
            [(f["start"], f["end"], f["comment"]) for f in frags],
            [(0, 3, "старт"), (5, 9, "финиш")])

    def test_position_line_applied(self):
        frags, position, problem = self.parse(b"# position\t7\n" + TSV_FIXTURE)
        self.assertIsNone(problem)
        self.assertEqual(position, 7)
        self.assertEqual(len(frags), 2)

    def test_malformed_position_rejected(self):
        _, _, problem = self.parse(b"# position\tabc\n" + TSV_FIXTURE)
        self.assertEqual(problem[0], 400)
        self.assertIn("позиции", problem[1])

    def test_non_utf8_rejected(self):
        _, _, problem = self.parse(b"\xff\xfe start\tend\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("UTF-8", problem[1])

    def test_missing_header_rejected(self):
        _, _, problem = self.parse(b"0\t3\tx\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("start/end", problem[1])

    def test_non_numeric_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\nabc\t3\tx\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("Нечисловые", problem[1])

    def test_out_of_range_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\n5\t99\tx\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("вне диапазона", problem[1])

    def test_negative_start_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\n-1\t3\tx\n")
        self.assertEqual(problem[0], 400)

    def test_start_after_end_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\n7\t2\tx\n")
        self.assertEqual(problem[0], 400)

    def test_overlap_rejected_409(self):
        data = b"start\tend\tcomment\n0\t5\ta\n3\t8\tb\n"
        _, _, problem = self.parse(data)
        self.assertEqual(problem[0], 409)
        self.assertIn("пересекаются", problem[1])

    def test_touching_fragments_allowed(self):
        # 0-3 и 4-9 не пересекаются: границы кадров разные.
        frags, _, problem = self.parse(
            b"start\tend\tcomment\n0\t3\ta\n4\t9\tb\n")
        self.assertIsNone(problem)
        self.assertEqual(len(frags), 2)

    def test_same_frame_boundary_is_overlap(self):
        # Общий кадр 3 — уже пересечение (как в PUT /fragments).
        _, _, problem = self.parse(
            b"start\tend\tcomment\n0\t3\ta\n3\t5\tb\n")
        self.assertEqual(problem[0], 409)

    def test_position_out_of_range_rejected(self):
        _, _, problem = self.parse(b"# position\t10\n" + TSV_FIXTURE)
        self.assertEqual(problem[0], 400)
        self.assertIn("Позиция", problem[1])

    def test_extra_columns_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\n0\t3\tx\ty\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("Лишние", problem[1])

    def test_empty_input_is_empty_ok(self):
        for data in (b"start\tend\tcomment\n", b""):
            with self.subTest(data=data):
                frags, position, problem = self.parse(data)
                self.assertIsNone(problem)
                self.assertEqual((frags, position), ([], 0))

    def test_unknown_total_skips_upper_bound(self):
        # total_frames=0 — верхняя граница не проверяется (как в PUT).
        frags, _, problem = self.parse(
            b"start\tend\tcomment\n0\t999999\tx\n", total=0)
        self.assertIsNone(problem)
        self.assertEqual(len(frags), 1)

    def test_nul_byte_rejected(self):
        _, _, problem = self.parse(b"start\tend\tcomment\n0\t3\ta\x00b\n")
        self.assertEqual(problem[0], 400)
        self.assertIn("недопустимый символ", problem[1])


# ─── GET: бэкап задачи ───────────────────────────────────────────────────────

class TaskBackupGetTests(BackupArchiveMixin, WorkspaceApiTestBase):
    """GET workspaces/<id>/backup: состав архива, заголовки, отказы."""

    def setUp(self):
        super().setUp()
        self.backup_url = f"/api/v1/workspaces/{self.ws_id}/backup"
        # Прогрев индекса: отпечаток в манифесте, без фоновой сборки.
        frame_provider.get_metadata(self.video)

    def test_backup_snapshot_members_and_manifest(self):
        resp = self.client.get(self.backup_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("test-ws-backup.zip", resp["Content-Disposition"])
        data = fetch_zip(resp)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertEqual(
                names, {"manifest.json", "task.json", "fragments.tsv",
                        "visualization.mp4"})
            manifest = json.loads(zf.read(fmt.MANIFEST_NAME))
            self.assertEqual(manifest["format"], fmt.FORMAT_NAME)
            self.assertEqual(manifest["kind"], fmt.KIND_BACKUP)
            self.assertEqual(manifest["resource"], fmt.RESOURCE_TASK)
            self.assertEqual(manifest["task_id"], self.ws_id)
            # Первый скан внутри бэкапа: миграция привязала к default.
            self.assertEqual(manifest["project_id"],
                             project_meta.DEFAULT_PROJECT_ID)
            self.assertEqual(manifest["media"]["total_frames"], 10)
            self.assertEqual(manifest["media"]["roles"]["preview"],
                             "visualization.mp4")
            # Видео — без сжатия (ZIP_STORED): быстрее и без потерь.
            self.assertEqual(zf.getinfo("visualization.mp4").compress_type,
                             zipfile.ZIP_STORED)
            with open(os.path.join(self.ws_dir, "fragments.tsv"), "rb") as fh:
                self.assertEqual(zf.read("fragments.tsv"), fh.read())
        self.assertEqual(int(resp["Content-Length"]), len(data))

    def test_backup_excludes_exports_and_dotfiles(self):
        os.makedirs(os.path.join(self.ws_dir, "exports"))
        with open(os.path.join(self.ws_dir, "exports", "cut.mp4"), "wb") as fh:
            fh.write(b"export")
        with open(os.path.join(self.ws_dir, ".part.mp4"), "wb") as fh:
            fh.write(b"partial")
        data = self.fetch_zip_url(self.backup_url)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            self.assertFalse([n for n in names if "exports" in n])
            self.assertFalse(
                [n for n in names if os.path.basename(n).startswith(".")])

    def test_backup_missing_404(self):
        resp = self.client.get("/api/v1/workspaces/no-such/backup")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("не найден", resp.json()["error"])

    def test_backup_trailing_slash_url(self):
        data = self.fetch_zip_url(self.backup_url + "/")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn(fmt.MANIFEST_NAME, zf.namelist())

    def test_backup_disk_full_507_leaves_no_temp(self):
        """Кончилось место: 507 с текстом, временный архив удалён."""
        # logging.disable: тестовый harness (py3.14 + Django 4.2) падает
        # при логгинге любого 5xx-ответа — гасим логи на время проверки.
        nospace = OSError(errno.ENOSPC, "No space left on device")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch("zipfile.ZipFile.writestr", side_effect=nospace):
                resp = self.client.get(self.backup_url)
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)
        self.assertIn("места", resp.json()["error"])
        self.assertEqual(
            [e for e in os.listdir(self._tmpdir) if e.startswith(".backup-")],
            [])

    def test_backup_sweeps_stale_temp_archives(self):
        """Сирота .backup-* (падение без закрытия) чистится при сборке."""
        stale = os.path.join(self._tmpdir, ".backup-dead.zip")
        fresh = os.path.join(self._tmpdir, ".backup-live.zip")
        for path in (stale, fresh):
            with open(path, "wb") as fh:
                fh.write(b"orphan")
        old = time.time() - 48 * 3600
        os.utime(stale, (old, old))
        resp = self.client.get(self.backup_url)
        self.assertEqual(resp.status_code, 200)
        fetch_zip(resp)
        left = {e for e in os.listdir(self._tmpdir)
                if e.startswith(".backup-")}
        self.assertNotIn(".backup-dead.zip", left)
        self.assertIn(".backup-live.zip", left)


# ─── POST: импорт задачи ─────────────────────────────────────────────────────

class TaskImportTests(BackupArchiveMixin, ProjectTestBase):
    """POST backups/import: on_conflict, отказы, стейджинг, членство.

    База — ProjectTestBase (нужны create_project/json_post для ``?project``).
    """

    def setUp(self):
        super().setUp()
        self.import_url = "/api/v1/backups/import"
        self.new_id = f"{self.ws_id}_1"
        self.new_dir = os.path.join(self._tmpdir, self.new_id)
        frame_provider.get_metadata(self.video)
        # Первый скан через GET backup: миграция кладёт задачу в default,
        # манифест архива фиксирует это членство.
        self.archive = self.fetch_zip_url(
            f"/api/v1/workspaces/{self.ws_id}/backup")

    def test_import_default_is_rename(self):
        resp = self.import_archive(self.archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["kind"], fmt.KIND_BACKUP)
        self.assertEqual(body["id"], self.new_id)
        self.assertEqual(body["project_id"], project_meta.DEFAULT_PROJECT_ID)
        self.assertTrue(os.path.isfile(
            os.path.join(self.new_dir, "visualization.mp4")))
        ids = [w["id"] for w in self.client.get(self.ws_list_url).json()]
        self.assertIn(self.new_id, ids)
        frags = self.client.get(
            f"/api/v1/pairs/{self.new_id}/fragments/").json()
        self.assertEqual(len(frags), 2)
        # Оригинал не тронут.
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)
        meta = _wait_validation(self, self.new_id, self.new_dir)
        self.assertEqual(meta["project_id"],
                         project_meta.DEFAULT_PROJECT_ID)

    def test_import_error_conflict_409(self):
        resp = self.import_archive(self.archive, "?on_conflict=error")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("уже существует", resp.json()["error"])
        self.assertFalse(os.path.isdir(self.new_dir))
        # Отказ до стейджинга: служебных папок не появилось.
        self.assertFalse([e for e in os.listdir(self._tmpdir)
                          if e.startswith(".import-")])

    def test_import_error_mode_ok_when_name_free(self):
        data = rewrite_manifest(
            self.archive, lambda m: m.update(task_id="fresh-import"))
        resp = self.import_archive(data, "?on_conflict=error")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["id"], "fresh-import")
        _wait_validation(
            self, "fresh-import",
            os.path.join(self._tmpdir, "fresh-import"))

    def test_import_rename_skips_occupied_suffix(self):
        os.makedirs(self.new_dir)  # занято — суффикс обязан уйти
        resp = self.import_archive(self.archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["id"], "test-ws_2")
        _wait_validation(
            self, "test-ws_2", os.path.join(self._tmpdir, "test-ws_2"))

    def test_import_overwrite_replaces_in_place(self):
        # Правим разметку — импорт вернёт состояние архива.
        resp = self.json_put(self.frags_url,
                             {"fragments": [{"start": 0, "end": 1}],
                              "position": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.import_archive(self.archive, "?on_conflict=overwrite")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["id"], self.ws_id)
        self.assertFalse(os.path.isdir(self.new_dir))
        self.assertEqual(
            len(self.client.get(self.frags_url).json()), 2)
        meta = _wait_validation(self, self.ws_id, self.ws_dir)
        self.assertEqual(meta["project_id"],
                         project_meta.DEFAULT_PROJECT_ID)

    def test_import_invalid_conflict_400(self):
        resp = self.import_archive(self.archive, "?on_conflict=merge")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("on_conflict", resp.json()["error"])

    def test_import_missing_file_400(self):
        resp = self.client.post(self.import_url)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("архива", resp.json()["error"])

    def test_import_not_zip_400(self):
        resp = self.import_archive(b"just plain text, not an archive")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("zip", resp.json()["error"])

    def test_import_foreign_manifest_400(self):
        data = build_zip({"manifest.json": manifest_json(format="cvat"),
                          "visualization.mp4": b"x"})
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("не архив VideoCutter", resp.json()["error"])
        self.assertFalse(os.path.isdir(self.new_dir))

    def test_import_missing_manifest_400(self):
        data = build_zip({"visualization.mp4": b"x"})
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("нет manifest.json", resp.json()["error"])

    def test_import_zip_slip_400(self):
        data = build_zip({"manifest.json": manifest_json(task_id=self.ws_id),
                          "../evil.txt": b"pwn",
                          "visualization.mp4": b"x"})
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Небезопасное", resp.json()["error"])
        # Ни вышедшего за корень файла, ни служебного стейджинга.
        self.assertFalse(os.path.exists(
            os.path.join(os.path.dirname(self._tmpdir), "evil.txt")))
        self.assertFalse([e for e in os.listdir(self._tmpdir)
                          if e.startswith(".import-")])

    def test_import_task_without_video_400(self):
        data = build_zip({"manifest.json": manifest_json(task_id="ghost"),
                          "fragments.tsv": TSV_FIXTURE})
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("нет видео", resp.json()["error"])
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, "ghost")))

    def test_import_annotations_archive_hints_annotations_api(self):
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": TSV_FIXTURE})
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Импорт разметки", resp.json()["error"])

    def test_import_project_param_rebinds(self):
        self.create_project("Приём")
        resp = self.import_archive(self.archive, "?project=" + u("Приём"))
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertEqual(resp.json()["project_id"], "Приём")
        meta = _wait_validation(self, self.new_id, self.new_dir)
        self.assertEqual(meta["project_id"], "Приём")
        # Оригинал остался в default.
        self.assertEqual(task_meta.load(self.ws_dir)["project_id"],
                         project_meta.DEFAULT_PROJECT_ID)

    def test_import_project_param_missing_400(self):
        resp = self.import_archive(self.archive, "?project=no-such")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("не найден", resp.json()["error"])
        self.assertFalse(os.path.isdir(self.new_dir))

    def test_import_disk_full_507_cleans_staging(self):
        nospace = OSError(errno.ENOSPC, "No space left on device")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch("vc_pairs.backup.ws_fs.write_stream",
                            side_effect=nospace):
                resp = self.import_archive(self.archive)
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)
        self.assertIn("места", resp.json()["error"])
        # Ни частичной задачи, ни осиротевшего стейджинга.
        self.assertFalse(os.path.isdir(self.new_dir))
        self.assertFalse([e for e in os.listdir(self._tmpdir)
                          if e.startswith(".import-")])

    def test_import_sweeps_stale_staging(self):
        stale = os.path.join(self._tmpdir, ".import-dead")
        fresh = os.path.join(self._tmpdir, ".import-live")
        os.makedirs(stale)
        os.makedirs(fresh)
        old = time.time() - 48 * 3600
        os.utime(stale, (old, old))
        resp = self.import_archive(self.archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        # Старый стейджинг вычищен, живой (свежий mtime) — не тронут,
        # собственный стейджинг размещён (ушёл из корня).
        self.assertEqual(
            {e for e in os.listdir(self._tmpdir)
             if e.startswith(".import-")},
            {".import-live"})
        _wait_validation(self, self.new_id, self.new_dir)

    def test_imported_task_passes_validation(self):
        resp = self.import_archive(self.archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = _wait_ready(self, self.new_id)
        self.assertFalse(body["broken"])
        self.assertGreater(body["total_frames"], 0)
        meta = _wait_validation(self, self.new_id, self.new_dir)
        self.assertEqual(meta["project_id"],
                         project_meta.DEFAULT_PROJECT_ID)

    def test_import_standalone_keeps_default_out(self):
        """Явный standalone: архив без членства — импорт не влипает в default."""
        resp = self.client.delete(
            f"/api/v1/projects/{project_meta.DEFAULT_PROJECT_ID}"
            f"/tasks/{self.ws_id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        archive = self.fetch_zip_url(
            f"/api/v1/workspaces/{self.ws_id}/backup")
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            manifest = json.loads(zf.read(fmt.MANIFEST_NAME))
        self.assertIsNone(manifest["project_id"])
        resp = self.import_archive(archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["id"], self.new_id)
        self.assertIsNone(body["project_id"])
        meta = _wait_validation(self, self.new_id, self.new_dir)
        # default существует — но членство по умолчанию не применялось.
        self.assertTrue(
            project_meta.exists(project_meta.DEFAULT_PROJECT_ID))
        self.assertIsNone(meta["project_id"])

    def test_import_preserves_passport(self):
        meta = task_meta.load(self.ws_dir)
        meta["status"] = "completed"
        meta["owner"] = "алиса"
        task_meta.save(self.ws_dir, meta)
        archive = self.fetch_zip_url(
            f"/api/v1/workspaces/{self.ws_id}/backup")
        resp = self.import_archive(archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        new_meta = _wait_validation(self, self.new_id, self.new_dir)
        # Паспорт перенесён, а валидация дописала created_at сверху.
        self.assertEqual(new_meta["status"], "completed")
        self.assertEqual(new_meta["owner"], "алиса")
        self.assertTrue(new_meta["created_at"])

    def test_import_trailing_slash_url(self):
        resp = self.import_archive(self.archive, path=self.import_url + "/")
        self.assertEqual(resp.status_code, 202, resp.content)
        _wait_validation(self, self.new_id, self.new_dir)


# ─── Бэкап и импорт проекта ──────────────────────────────────────────────────

class ProjectBackupTests(BackupArchiveMixin, ProjectTestBase):
    """GET projects/<pid>/backup и восстановление бэкапа проекта."""

    def setUp(self):
        super().setUp()
        self.pid = "Архив"
        self.client.get(self.ws_list_url)  # первый скан → миграция в default
        self.create_project(self.pid)
        resp = self.json_post(self.tasks_url(self.pid),
                              {"task_id": self.ws_id})
        self.assertEqual(resp.status_code, 200, resp.content)
        frame_provider.get_metadata(self.video)
        self.project_backup_url = f"/api/v1/projects/{u(self.pid)}/backup"

    def _project_archive(self) -> bytes:
        return self.fetch_zip_url(self.project_backup_url)

    def test_project_backup_snapshot(self):
        os.makedirs(os.path.join(self.ws_dir, "exports"))
        with open(os.path.join(self.ws_dir, "exports", "cut.mp4"), "wb") as fh:
            fh.write(b"export")
        with open(os.path.join(self.ws_dir, ".part.mp4"), "wb") as fh:
            fh.write(b"partial")
        resp = self.client.get(self.project_backup_url)
        self.assertEqual(resp.status_code, 200)
        data = fetch_zip(resp)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertIn(fmt.MANIFEST_NAME, names)
            self.assertIn(project_meta.PROJECT_FILE, names)
            self.assertIn("task_0/task.json", names)
            self.assertIn("task_0/fragments.tsv", names)
            self.assertIn("task_0/visualization.mp4", names)
            # Служебное в задачах — не в архиве.
            self.assertFalse([n for n in names if "exports" in n])
            self.assertFalse(
                [n for n in names if os.path.basename(n).startswith(".")])
            manifest = json.loads(zf.read(fmt.MANIFEST_NAME))
            self.assertEqual(manifest["resource"], fmt.RESOURCE_PROJECT)
            self.assertEqual(manifest["project_id"], self.pid)
            self.assertEqual(manifest["tasks"],
                             [{"dir": "task_0", "task_id": self.ws_id}])
            project = json.loads(zf.read(project_meta.PROJECT_FILE))
            self.assertEqual(project["id"], self.pid)

    def test_project_backup_missing_404(self):
        resp = self.client.get("/api/v1/projects/no-such/backup")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("не найден", resp.json()["error"])

    def test_project_backup_trailing_slash_url(self):
        data = self.fetch_zip_url(self.project_backup_url + "/")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn(project_meta.PROJECT_FILE, zf.namelist())

    def test_project_import_rename_creates_copy(self):
        archive = self._project_archive()
        resp = self.import_archive(archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["kind"], fmt.KIND_BACKUP)
        self.assertEqual(body["id"], "Архив_1")
        self.assertEqual(body["tasks"], [f"{self.ws_id}_1"])
        self.assertTrue(project_meta.exists("Архив_1"))
        new_task_dir = os.path.join(self._tmpdir, f"{self.ws_id}_1")
        self.assertEqual(task_meta.load(new_task_dir)["project_id"],
                         "Архив_1")
        # Оригиналы целы.
        self.assertTrue(project_meta.exists(self.pid))
        self.assertEqual(task_meta.load(self.ws_dir)["project_id"], self.pid)
        # Список проектов видит копию с её задачей.
        by_id = {p["id"]: p for p in self.client.get(self.projects_url).json()}
        self.assertEqual(by_id["Архив_1"]["task_count"], 1)
        _wait_validation(self, f"{self.ws_id}_1", new_task_dir)

    def _make_second_task(self) -> str:
        """Вторая задача проекта: другое имя видео, другая разметка."""
        second_id = "second-ws"
        second_dir = os.path.join(self._tmpdir, second_id)
        os.makedirs(second_dir, exist_ok=True)
        with open(self.video, "rb") as src:
            video_bytes = src.read()
        with open(os.path.join(second_dir, "clip.mp4"), "wb") as fh:
            fh.write(video_bytes)
        with open(os.path.join(second_dir, "fragments.tsv"), "w") as fh:
            fh.write("start\tend\tcomment\n1\t2\tвторая\n")
        self.client.get(self.ws_list_url)  # перескан: подбор новой папки
        resp = self.json_post(self.tasks_url(self.pid), {"task_id": second_id})
        self.assertEqual(resp.status_code, 200, resp.content)
        return second_id

    def _dir_snapshot(self, ws_id: str):
        """Состав папки задачи: {файлы} + байты видео + разметка через API."""
        path = os.path.join(self._tmpdir, ws_id)
        videos = {}
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and name.endswith(".mp4"):
                with open(full, "rb") as fh:
                    videos[name] = fh.read()
        frags = self.client.get(f"/api/v1/pairs/{ws_id}/fragments/").json()
        return videos, [(f["start"], f["end"]) for f in frags]

    def test_project_roundtrip_two_tasks_keep_identity(self):
        """Регрессия: вторая задача — копия второй, а не дубль первой."""
        second_id = self._make_second_task()
        before = {self.ws_id: self._dir_snapshot(self.ws_id),
                  second_id: self._dir_snapshot(second_id)}
        self.assertNotEqual(before[self.ws_id], before[second_id])
        archive = self._project_archive()
        resp = self.import_archive(archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["id"], "Архив_1")
        self.assertEqual(len(body["tasks"]), 2)
        after = {tid: self._dir_snapshot(tid) for tid in body["tasks"]}
        # Каждая восстановленная задача совпадает со своим оригиналом
        # (сравнение через repr: состав несравнимых dict напрямую).
        self.assertEqual(sorted(map(repr, after.values())),
                         sorted(map(repr, before.values())))
        for tid in body["tasks"]:
            _wait_validation(
                self, tid, os.path.join(self._tmpdir, tid))
            self.assertEqual(task_meta.load(
                os.path.join(self._tmpdir, tid))["project_id"], "Архив_1")

    def test_project_import_overwrite_reuses_project(self):
        archive = self._project_archive()
        resp = self.json_put(self.frags_url,
                             {"fragments": [{"start": 0, "end": 1}],
                              "position": 0})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.import_archive(archive, "?on_conflict=overwrite")
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["id"], self.pid)
        self.assertEqual(body["tasks"], [self.ws_id])
        # Ни дублей проекта, ни переименованной задачи.
        self.assertFalse(project_meta.exists("Архив_1"))
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, f"{self.ws_id}_1")))
        self.assertEqual(
            len(self.client.get(self.frags_url).json()), 2)
        _wait_validation(self, self.ws_id, self.ws_dir)

    def test_project_import_error_conflict_leaves_no_container(self):
        archive = self._project_archive()
        # Имя проекта освобождаем (keep — задачи не удаляем), имя задачи
        # занято: в error-режиме контейнер создаваться не должен.
        resp = self.client.delete(
            f"{self.project_url(self.pid)}?with_tasks=keep")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(project_meta.exists(self.pid))
        resp = self.import_archive(archive, "?on_conflict=error")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("уже существует", resp.json()["error"])
        self.assertFalse(project_meta.exists(self.pid))
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, f"{self.ws_id}_1")))

    def test_project_import_empty_project_roundtrip(self):
        self.create_project("Пустой")
        archive = self.fetch_zip_url(
            f"/api/v1/projects/{u('Пустой')}/backup")
        resp = self.import_archive(archive)
        self.assertEqual(resp.status_code, 202, resp.content)
        body = resp.json()
        self.assertEqual(body["id"], "Пустой_1")
        self.assertEqual(body["tasks"], [])
        self.assertTrue(project_meta.exists("Пустой_1"))

    def test_project_import_rejects_project_param(self):
        self.create_project("Приём")
        archive = self._project_archive()
        resp = self.import_archive(archive, "?project=" + u("Приём"))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("не применяется", resp.json()["error"])
        # Ничего не создано.
        self.assertFalse(project_meta.exists("Архив_1"))
        self.assertFalse(os.path.isdir(
            os.path.join(self._tmpdir, f"{self.ws_id}_1")))

    def test_project_import_rejects_flat_video(self):
        manifest = fmt.make_manifest(
            fmt.KIND_BACKUP, resource=fmt.RESOURCE_PROJECT,
            project_id="Призрак", tasks=[])
        data = build_zip({
            "manifest.json": json.dumps(manifest, ensure_ascii=False).encode(
                "utf-8"),
            "visualization.mp4": b"x",
        })
        resp = self.import_archive(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("бэкап задачи", resp.json()["error"])
        self.assertFalse(project_meta.exists("Призрак"))


# ─── Экспорт/импорт разметки ─────────────────────────────────────────────────

class AnnotationsTests(BackupArchiveMixin, WorkspaceApiTestBase):
    """GET/POST pairs/<id>/annotations: экспорт, отпечаток, структура."""

    def setUp(self):
        super().setUp()
        self.ann_url = f"/api/v1/pairs/{self.ws_id}/annotations"
        # Прогрев индекса: отпечаток в манифесте, сверка работает.
        frame_provider.get_metadata(self.video)

    def _import(self, data: bytes, query: str = "", trailing: bool = False):
        suffix = "/" if trailing else ""
        return self.import_archive(
            data, query, path=f"{self.ann_url}/import{suffix}")

    def test_export_without_media(self):
        resp = self.client.get(self.ann_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("test-ws-annotations.zip", resp["Content-Disposition"])
        data = fetch_zip(resp)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            self.assertEqual(names, {"manifest.json", "fragments.tsv"})
            manifest = json.loads(zf.read(fmt.MANIFEST_NAME))
            self.assertEqual(manifest["format"], fmt.FORMAT_NAME)
            self.assertEqual(manifest["kind"], fmt.KIND_ANNOTATIONS)
            self.assertEqual(manifest["resource"], fmt.RESOURCE_TASK)
            self.assertEqual(manifest["task_id"], self.ws_id)
            self.assertEqual(manifest["media"]["total_frames"], 10)
            self.assertEqual(manifest["media"]["roles"]["preview"],
                             "visualization.mp4")
            with open(os.path.join(self.ws_dir, "fragments.tsv"),
                      "rb") as fh:
                self.assertEqual(zf.read("fragments.tsv"), fh.read())
        self.assertEqual(int(resp["Content-Length"]), len(data))

    def test_export_with_media(self):
        data = self.fetch_zip_url(self.ann_url + "?media=1")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("visualization.mp4", zf.namelist())
            with open(self.video, "rb") as fh:
                self.assertEqual(zf.read("visualization.mp4"), fh.read())

    def test_export_missing_404(self):
        resp = self.client.get("/api/v1/pairs/no-such/annotations")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("не найден", resp.json()["error"])

    def test_export_without_tsv_writes_header(self):
        os.remove(os.path.join(self.ws_dir, "fragments.tsv"))
        data = self.fetch_zip_url(self.ann_url)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Без разметки — пустой валидный файл: roundtrip работает.
            self.assertEqual(zf.read("fragments.tsv"),
                             b"start\tend\tcomment\n")

    def test_import_roundtrip_restores_fragments(self):
        archive = self.fetch_zip_url(self.ann_url)
        # Ломаем разметку — импорт вернёт состояние архива.
        resp = self.json_put(self.frags_url,
                             {"fragments": [{"start": 0, "end": 1}],
                              "position": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._import(archive)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(),
                         {"task_id": self.ws_id, "fragments": 2,
                          "position": 0})
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)
        self.assertEqual(
            self.client.get(self.position_url).json()["position"], 0)

    def test_import_applies_position(self):
        tsv = "# position\t7\nstart\tend\tcomment\n1\t4\tраз\n"
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": tsv.encode("utf-8")})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(),
                         {"task_id": self.ws_id, "fragments": 1,
                          "position": 7})
        frags = self.client.get(self.frags_url).json()
        self.assertEqual([(f["start"], f["end"]) for f in frags], [(1, 4)])
        self.assertEqual(
            self.client.get(self.position_url).json()["position"], 7)

    def test_import_fingerprint_mismatch_409_then_force(self):
        archive = self.fetch_zip_url(self.ann_url)
        mismatched = rewrite_manifest(
            archive, lambda m: m.update(media={"total_frames": 999}))
        resp = self.json_put(self.frags_url,
                             {"fragments": [{"start": 0, "end": 1}],
                              "position": 0})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._import(mismatched)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("другого видео", resp.json()["error"])
        # Разметка не применилась.
        self.assertEqual(len(self.client.get(self.frags_url).json()), 1)
        # force=1 — явное принятие риска.
        resp = self._import(mismatched, "?force=1")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)

    def test_import_overlap_409_keeps_task_intact(self):
        tsv = "start\tend\tcomment\n0\t5\ta\n3\t8\tb\n"
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": tsv.encode("utf-8")})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertIn("пересекаются", resp.json()["error"])
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)

    def test_import_out_of_range_400(self):
        tsv = b"start\tend\tcomment\n5\t99\tx\n"
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": tsv})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("вне диапазона", resp.json()["error"])
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)

    def test_import_position_out_of_range_400(self):
        tsv = b"# position\t10\nstart\tend\tcomment\n0\t3\tx\n"
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": tsv})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("Позиция", resp.json()["error"])

    def test_import_non_utf8_400(self):
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id),
             "fragments.tsv": b"\xff\xfe bad-bytes"})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("UTF-8", resp.json()["error"])

    def test_import_wrong_kind_400(self):
        data = build_zip({"manifest.json": manifest_json(task_id=self.ws_id),
                          "fragments.tsv": TSV_FIXTURE})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("kind=annotations", resp.json()["error"])

    def test_import_missing_fragments_member_400(self):
        data = build_zip(
            {"manifest.json": annotations_manifest_json(task_id=self.ws_id)})
        resp = self._import(data)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("нет fragments.tsv", resp.json()["error"])

    def test_import_missing_file_400(self):
        resp = self.client.post(self.ann_url + "/import")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("архива", resp.json()["error"])

    def test_import_missing_task_404(self):
        resp = self.client.post(
            "/api/v1/pairs/no-such/annotations/import",
            {"file": SimpleUploadedFile("x.zip", b"",
                                        content_type="application/zip")})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("не найден", resp.json()["error"])

    def test_import_disk_full_507_cleans_tmp(self):
        """Сбой записи: 507, временный файл рядом с целью удалён."""
        archive = self.fetch_zip_url(self.ann_url)
        nospace = OSError(errno.ENOSPC, "No space left on device")
        logging.disable(logging.CRITICAL)
        try:
            with mock.patch("vc_fragments.annotations.os.replace",
                            side_effect=nospace):
                resp = self._import(archive)
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(resp.status_code, 507)
        self.assertIn("места", resp.json()["error"])
        self.assertEqual(
            [e for e in os.listdir(self.ws_dir)
             if e.startswith(".fragments-import-")],
            [])
        # Разметка не тронута.
        self.assertEqual(len(self.client.get(self.frags_url).json()), 2)

    def test_import_trailing_slash_url(self):
        archive = self.fetch_zip_url(self.ann_url)
        resp = self._import(archive, trailing=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["fragments"], 2)
