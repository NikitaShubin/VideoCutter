# -*- coding: utf-8 -*-
"""Входной фильтр-токен (VC_AUTH_TOKEN): выключен и включён.

Прогон: cd backend && python manage.py test
"""

import os
from unittest import mock

from django.test import SimpleTestCase


def with_token(token: str | None):
    """Env-патч фильтра: заданный токен или выключено (пусто = выкл)."""
    return mock.patch.dict(
        os.environ, {"VC_AUTH_TOKEN": token or ""}, clear=False)


class AuthDisabledTests(SimpleTestCase):
    def test_status_reports_disabled(self):
        with with_token(None):
            resp = self.client.get("/api/v1/auth/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"enabled": False})

    def test_api_open_without_token(self):
        with with_token(None):
            resp = self.client.get("/api/v1/debug/threads")
        self.assertEqual(resp.status_code, 200)


class AuthEnabledTests(SimpleTestCase):
    SECRET = "s3cret-door"

    def test_status_reports_enabled_without_token(self):
        with with_token(self.SECRET):
            resp = self.client.get("/api/v1/auth/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"enabled": True})

    def test_api_rejects_anonymous(self):
        with with_token(self.SECRET):
            resp = self.client.get("/api/v1/debug/threads")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("токен", resp.json()["error"])

    def test_api_rejects_wrong_token(self):
        with with_token(self.SECRET):
            resp = self.client.get(
                "/api/v1/debug/threads",
                HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(resp.status_code, 401)

    def test_bearer_header_opens(self):
        with with_token(self.SECRET):
            resp = self.client.get(
                "/api/v1/debug/threads",
                HTTP_AUTHORIZATION=f"Bearer {self.SECRET}")
        self.assertEqual(resp.status_code, 200)

    def test_query_token_opens_for_media_links(self):
        # <img>/<a> заголовок не несут — токен query (кадры, скачивание).
        with with_token(self.SECRET):
            resp = self.client.get(
                f"/api/v1/debug/threads?token={self.SECRET}")
        self.assertEqual(resp.status_code, 200)
