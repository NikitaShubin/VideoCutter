# -*- coding: utf-8 -*-
"""Аудит P3: добивка покрытия старой функциональности.

Прогон: cd backend && python manage.py test

Маршруты без тестов на момент P3:
- ``pairs/<id>/export/<path>`` (fragment_export_download) — ни happy path,
  ни traversal-гейт (``../`` обязан давать 404, а не файл за пределами
  exports/);
- ``debug/threads`` — вообще без вызовов;
- ``workspaces/<id>/meta`` — happy path есть (test_meta), 404 нет.
"""

import os

from vc_fragments.tests import WorkspaceApiTestBase


class ExportDownloadTests(WorkspaceApiTestBase):
    """Скачивание готовых нарезок: файл, 404, защита от выхода из exports/."""

    def _exports_file(self, name: str = "frag_0.mp4",
                      data: bytes = b"fake-video-bytes") -> str:
        out = os.path.join(self.ws_dir, "exports")
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, name), "wb") as fh:
            fh.write(data)
        return data

    def _url(self, path: str) -> str:
        return f"/api/v1/pairs/{self.ws_id}/export/{path}"

    def test_download_returns_file_bytes(self):
        data = self._exports_file()
        resp = self.client.get(self._url("frag_0.mp4"))
        self.assertEqual(resp.status_code, 200)
        body = b"".join(resp.streaming_content)
        resp.close()
        self.assertEqual(body, data)

    def test_download_missing_file_404(self):
        resp = self.client.get(self._url("nope.mp4"))
        self.assertEqual(resp.status_code, 404)

    def test_download_missing_task_404(self):
        resp = self.client.get("/api/v1/pairs/no-such/export/frag_0.mp4")
        self.assertEqual(resp.status_code, 404)

    def test_download_traversal_blocked(self):
        """``../`` не выводит за exports/: вместо fragments.tsv — 404."""
        resp = self.client.get(self._url("../fragments.tsv"))
        self.assertEqual(resp.status_code, 404)


class WorkspaceMetaTests(WorkspaceApiTestBase):
    def test_meta_missing_404(self):
        resp = self.client.get("/api/v1/workspaces/no-such/meta")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("не найден", resp.json()["error"])


class DebugThreadsTests(WorkspaceApiTestBase):
    def test_debug_threads_snapshot_shape(self):
        resp = self.client.get("/api/v1/debug/threads")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        for key in ("decode_pool", "prefetch_pool", "sources", "stacks"):
            self.assertIn(key, body)
