# -*- coding: utf-8 -*-
"""Чистые тесты обвязки: роли видео в имени файла, сканер, swap, promote.

Прогон: cd backend && python manage.py test vc_pairs.tests_video_roles
"""

import os
import shutil
import tempfile

from django.test import SimpleTestCase

from videocutter.standalone import workspace as ws_fs


class VideoRolesFSTests(SimpleTestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.root = self._tmpdir
        self.ws = os.path.join(self._tmpdir, "ws")
        os.makedirs(self.ws)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, name: str, content: bytes = b"x") -> None:
        with open(os.path.join(self.ws, name), "wb") as f:
            f.write(content)

    def _files(self):
        return sorted(os.listdir(self.ws))

    # ── classify ────────────────────────────────────────────────────────────

    def test_classify_exact_pair(self):
        self._write("a_source.mp4")
        self._write("b_preview.mp4")
        got = ws_fs.classify_videos(self.root, "ws")
        self.assertEqual(os.path.basename(got["source"]), "a_source.mp4")
        self.assertEqual(os.path.basename(got["preview"]), "b_preview.mp4")
        self.assertEqual([os.path.basename(p) for p in got["unassigned"]], [])

    def test_classify_legacy_viz(self):
        """Старые flat-workspaces с _viz остаются совместимыми."""
        self._write("clip.mp4")
        self._write("clip_viz.mp4")
        got = ws_fs.classify_videos(self.root, "ws")
        self.assertEqual(os.path.basename(got["source"]), "clip.mp4")
        self.assertEqual(os.path.basename(got["preview"]), "clip_viz.mp4")

    def test_classify_single_neutral_acts_as_both(self):
        self._write("alone.mp4")
        got = ws_fs.classify_videos(self.root, "ws")
        self.assertEqual(got["source"], got["preview"])
        self.assertEqual(os.path.basename(got["source"]), "alone.mp4")
        self.assertEqual(got["unassigned"], [])

    def test_classify_two_neutrals_pair_first(self):
        self._write("a.mp4")
        self._write("b.mp4")
        got = ws_fs.classify_videos(self.root, "ws")
        self.assertEqual(os.path.basename(got["source"]), "a.mp4")
        self.assertEqual(os.path.basename(got["preview"]), "a.mp4")
        self.assertEqual([os.path.basename(p) for p in got["unassigned"]], ["b.mp4"])

    def test_classify_exact_preview_with_neutral_source(self):
        """Превью задано маркером, нейтральный файл — источник (случай add)."""
        self._write("a.mp4")
        self._write("a_preview.mp4")
        got = ws_fs.classify_videos(self.root, "ws")
        self.assertEqual(os.path.basename(got["source"]), "a.mp4")
        self.assertEqual(os.path.basename(got["preview"]), "a_preview.mp4")

    # ── role_filename ───────────────────────────────────────────────────────

    def test_role_filename_unified(self):
        """Новые файлы получают ролевые имена source/preview (имя файла = роль)."""
        self.assertEqual(ws_fs.role_filename("6.avi", "source"), "source.avi")
        self.assertEqual(ws_fs.role_filename("6_preview.mp4", "preview"), "preview.mp4")
        self.assertEqual(ws_fs.role_filename("video_source.mp4", "source"), "source.mp4")
        # Сканер читает и унифицированные, и старые суффиксные имена.
        self.assertEqual(ws_fs.role_of_name("source.avi"), "source")
        self.assertEqual(ws_fs.role_of_name("preview.mp4"), "preview")
        self.assertEqual(ws_fs.role_of_name("clip_preview.mp4"), "preview")
        self.assertEqual(ws_fs.role_of_name("a.mp4"), None)
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.role_filename("a.mp4", "bogus")

    # ── promote_plain_video ─────────────────────────────────────────────────

    def test_promote_plain_video(self):
        self._write("neut.mp4")
        new = ws_fs.promote_plain_video(self.root, "ws", "source")
        self.assertEqual(os.path.basename(new), "source.mp4")
        self.assertIn("source.mp4", self._files())
        self.assertNotIn("neut.mp4", self._files())
        self.assertIsNone(ws_fs.promote_plain_video(self.root, "ws", "preview"))

    # ── swap ────────────────────────────────────────────────────────────────

    def test_swap_exchanges_roles(self):
        self._write("foo_source.mp4", b"SRC")
        self._write("bar_preview.mp4", b"PVW")
        new_s, new_p = ws_fs.swap_role_files(self.root, "ws")
        self.assertEqual(os.path.basename(new_s), "source.mp4")
        self.assertEqual(os.path.basename(new_p), "preview.mp4")
        with open(os.path.join(self.ws, "source.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"PVW")
        with open(os.path.join(self.ws, "preview.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"SRC")

    def test_swap_preserves_extensions(self):
        self._write("v1_source.mp4", b"SRC")
        self._write("v2_preview.webm", b"PVW")
        ws_fs.swap_role_files(self.root, "ws")
        with open(os.path.join(self.ws, "preview.mp4"), "rb") as f:
            self.assertEqual(f.read(), b"SRC")
        with open(os.path.join(self.ws, "source.webm"), "rb") as f:
            self.assertEqual(f.read(), b"PVW")
        self.assertNotIn("v2_preview.webm", self._files())

    def test_swap_requires_two_roles(self):
        self._write("only_source.mp4")
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.swap_role_files(self.root, "ws")

    # ── commit_role_upload ──────────────────────────────────────────────────

    def test_commit_role_upload_replaces_old_role_file(self):
        self._write("old_source.mp4", b"OLD")
        tmp = ws_fs.make_upload_temp(self.root, "ws")
        ws_fs.write_stream(open(__file__, "rb"), tmp)
        final = ws_fs.commit_role_upload(self.root, "ws", "source", tmp, "new.mp4")
        self.assertEqual(os.path.basename(final), "source.mp4")
        self.assertIn("source.mp4", self._files())
        self.assertNotIn("old_source.mp4", self._files())
        self.assertNotIn(os.path.basename(tmp), self._files())

    def test_commit_role_upload_overwrites_same_name(self):
        self._write("source.mp4", b"SAME")
        tmp = ws_fs.make_upload_temp(self.root, "ws")
        with open(tmp, "wb") as f:
            f.write(b"NEW")
        final = ws_fs.commit_role_upload(self.root, "ws", "source", tmp, "a.avi")
        self.assertEqual(os.path.basename(final), "source.avi")
        with open(os.path.join(self.ws, "source.avi"), "rb") as f:
            self.assertEqual(f.read(), b"NEW")
        self.assertNotIn("source.mp4", self._files())

    # ── assign_role_file ────────────────────────────────────────────────────

    def test_assign_role_file(self):
        self._write("neut.mp4")
        new = ws_fs.assign_role_file(self.root, "ws", "preview", "neut.mp4")
        self.assertEqual(os.path.basename(new), "preview.mp4")
        self.assertIn("preview.mp4", self._files())
        self.assertNotIn("neut.mp4", self._files())
        # Повторное назначение уже размеченного файла — ошибка.
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.assign_role_file(self.root, "ws", "source", "preview.mp4")

    def test_assign_role_file_rejects_taken_role(self):
        self._write("x_source.mp4")
        self._write("y.mp4")
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.assign_role_file(self.root, "ws", "source", "y.mp4")
        self.assertEqual(ws_fs.assign_role_file(self.root, "ws", "preview", "y.mp4"),
                         os.path.join(self.ws, "preview.mp4"))

    def test_assign_role_file_missing(self):
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.assign_role_file(self.root, "ws", "source", "gone.mp4")
        with self.assertRaises(ws_fs.InvalidWorkspaceError):
            ws_fs.assign_role_file(self.root, "ws", "bogus", "a.mp4")

    # ── расширения видео ────────────────────────────────────────────────────

    def test_video_exts_cover_common_formats(self):
        for ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".mts", ".m2ts", ".m4v"):
            self.assertIn(ext, ws_fs.VIDEO_EXTS)