# -*- coding: utf-8 -*-
"""Права доступа к exports/: другие пользователи хоста читают и копируют.

Прогон: cd backend && python manage.py test vc_fragments.tests_export_access
"""

import os
import shutil
import stat
import tempfile
from unittest import mock

from django.test import SimpleTestCase

import export_access
from vc_fragments.tests import WorkspaceApiTestBase


class ExportAccessTestBase(SimpleTestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="vcexportaccess_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.ws = os.path.join(self.root, "task")
        self.out = os.path.join(self.ws, "exports")
        os.makedirs(self.out)

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def _write(self, path, data=b"x"):
        with open(path, "wb") as f:
            f.write(data)
        return path


class EnsureAccessTests(ExportAccessTestBase):
    def test_opens_restricted_ancestors_for_traversal_only(self):
        """0700 у workspace и корня → 0711 (o+x без o+r), exports — 0755."""
        os.chmod(self.ws, 0o700)
        os.chmod(self.root, 0o700)

        export_access.ensure_access(self.ws, root=self.root)

        self.assertEqual(self._mode(self.ws), 0o711)
        self.assertEqual(self._mode(self.root), 0o711)
        self.assertEqual(self._mode(self.out), 0o755)
        self.assertEqual(self._mode(self.ws) & stat.S_IROTH, 0)

    def test_keeps_existing_world_read_bits(self):
        """Права других не сужаются: 0755 остаётся 0755, 0750 → 0751."""
        os.chmod(self.root, 0o755)
        os.chmod(self.ws, 0o750)

        export_access.ensure_access(self.ws, root=self.root)

        self.assertEqual(self._mode(self.root), 0o755)
        self.assertEqual(self._mode(self.ws), 0o751)

    def test_creates_exports_directory(self):
        shutil.rmtree(self.out)

        export_access.ensure_access(self.ws, root=self.root)

        self.assertTrue(os.path.isdir(self.out))
        self.assertEqual(self._mode(self.out), 0o755)

    def test_is_idempotent(self):
        os.chmod(self.ws, 0o700)

        export_access.ensure_access(self.ws, root=self.root)
        export_access.ensure_access(self.ws, root=self.root)

        self.assertEqual(self._mode(self.ws), 0o711)
        self.assertEqual(self._mode(self.out), 0o755)

    def test_missing_workspace_path_does_not_raise(self):
        """Несуществующий путь пропускается, а предки всё равно чинятся."""
        os.chmod(self.root, 0o700)

        export_access.ensure_ancestors(
            os.path.join(self.root, "missing"), root=self.root)

        self.assertEqual(self._mode(self.root), 0o711)

    def test_walk_boundary_at_filesystem_root(self):
        """Если stop не предок, обход останавливается на корне файловой системы."""
        export_access.ensure_ancestors(
            os.sep, root=os.path.join(self.root, "missing"))


class RepairExportsTests(ExportAccessTestBase):
    def test_repairs_old_file_and_sidecar_modes(self):
        """Старые нарезки и sidecar становятся 0644."""
        old = self._write(os.path.join(self.out, "video_fragment_1.mp4"))
        sidecar = self._write(os.path.join(self.out, ".export-state.json"))
        os.chmod(old, 0o666)
        os.chmod(sidecar, 0o600)

        repaired = export_access.repair_exports(self.root)

        self.assertEqual(repaired, 1)
        self.assertEqual(self._mode(old), 0o644)
        self.assertEqual(self._mode(sidecar), 0o644)

    def test_repairs_directory_modes(self):
        """Каталог exports — 0755, его предки получают o+x."""
        os.chmod(self.ws, 0o700)
        os.chmod(self.root, 0o700)

        export_access.repair_exports(self.root)

        self.assertEqual(self._mode(self.root), 0o711)
        self.assertEqual(self._mode(self.ws), 0o711)
        self.assertEqual(self._mode(self.out), 0o755)

    def test_keeps_partial_files_private(self):
        """Скрытый незавершённый файл остаётся 0600, а не публикуется."""
        partial = self._write(
            os.path.join(self.out, ".video_fragment_1.mp4"))
        os.chmod(partial, 0o644)

        export_access.repair_exports(self.root)

        self.assertEqual(self._mode(partial), 0o600)

    def test_preserves_foreign_files(self):
        foreign = self._write(os.path.join(self.out, "keep.txt"), b"keep")

        export_access.repair_exports(self.root)

        self.assertEqual(self._mode(foreign), 0o644)
        with open(foreign, "rb") as f:
            self.assertEqual(f.read(), b"keep")

    def test_does_not_follow_symlinks(self):
        """Symlink в exports не даёт менять права его цели."""
        target = self._write(os.path.join(self.root, "original.mp4"))
        os.chmod(target, 0o666)
        link = os.path.join(self.out, "link_fragment_1.mp4")
        try:
            os.symlink(target, link)
        except (AttributeError, OSError):
            self.skipTest("symlink недоступен")

        export_access.repair_exports(self.root)

        self.assertTrue(os.path.islink(link))
        self.assertEqual(self._mode(target), 0o666)

    def test_is_idempotent(self):
        old = self._write(os.path.join(self.out, "video_fragment_1.mp4"))
        os.chmod(old, 0o666)

        export_access.repair_exports(self.root)
        export_access.repair_exports(self.root)

        self.assertEqual(self._mode(old), 0o644)

    def test_chmod_oserror_does_not_abort(self):
        """Один неисправимый файл не мешает починить остальные."""
        bad = self._write(os.path.join(self.out, "video_fragment_1.mp4"))
        good = self._write(os.path.join(self.out, "video_fragment_2.mp4"))
        os.chmod(bad, 0o666)
        os.chmod(good, 0o666)
        real_chmod = os.chmod

        def chmod(path, mode):
            if os.path.abspath(path) == os.path.abspath(bad):
                raise OSError("ro")
            return real_chmod(path, mode)

        with mock.patch.object(export_access.os, "chmod", side_effect=chmod):
            export_access.repair_exports(self.root)

        self.assertEqual(self._mode(good), 0o644)


class RepairCliTests(ExportAccessTestBase):
    def test_cli_fix_returns_success(self):
        old = self._write(os.path.join(self.out, "video_fragment_1.mp4"))
        os.chmod(old, 0o666)

        code = export_access.main(["--fix", "--root", self.root])

        self.assertEqual(code, 0)
        self.assertEqual(self._mode(old), 0o644)

    def test_cli_requires_fix_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            export_access.main(["--root", self.root])
        self.assertEqual(ctx.exception.code, 2)

    def test_module_entrypoint_runs_fix(self):
        """`python -m export_access --fix` действительно чинит и выходит 0."""
        import runpy
        import sys

        old = self._write(os.path.join(self.out, "video_fragment_1.mp4"))
        os.chmod(old, 0o666)

        with mock.patch.object(
                sys, "argv",
                ["export_access", "--fix", "--root", self.root]):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_module("export_access", run_name="__main__")

        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(self._mode(old), 0o644)


class ExportAccessIntegrationTests(WorkspaceApiTestBase):
    """Новый экспорт сам открывает доступ и публикует артефакты 0644."""

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def _run_export(self):
        import vc_fragments.views as export_views

        export_views._run_export(self.ws_id)

    def test_export_opens_workspace_and_root_for_traversal(self):
        os.chmod(self.ws_dir, 0o700)
        os.chmod(self._tmpdir, 0o700)

        self._run_export()

        self.assertEqual(self._mode(self._tmpdir), 0o711)
        self.assertEqual(self._mode(self.ws_dir), 0o711)
        self.assertEqual(self._mode(os.path.join(self.ws_dir, "exports")),
                         0o755)

    def test_export_publishes_files_and_sidecar_world_readable(self):
        self._run_export()

        out_dir = os.path.join(self.ws_dir, "exports")
        names = sorted(os.listdir(out_dir))
        self.assertEqual(names, [
            ".export-state.json",
            "visualization_fragment_1.mp4",
            "visualization_fragment_2.mp4",
        ])
        for name in names:
            self.assertEqual(self._mode(os.path.join(out_dir, name)), 0o644)
