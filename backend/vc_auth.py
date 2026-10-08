# -*- coding: utf-8 -*-
"""Входной фильтр-токен для локальной обвязки (дверь, не учётка).

Один общий токен на всех, без логинов, ролей и сессий (это будет в VDO):
просто барьер, если сервис на машине с белым IP запущен в локальном
режиме — знающие токен хозяйничают, остальные мимо.

``VC_AUTH_TOKEN`` пуст (по умолчанию) — фильтра нет, всё как раньше.
Задан — любой запрос к ``/api/*`` (кроме самого статуса) требует тот же
токен: заголовок ``Authorization: Bearer`` или ``?token=`` (для ``<img>``
кадров и ``<a download>``, куда заголовок не прицепить). Иначе 401 JSON.

Хранилищ и протуханий нет: сверка — ``compare_digest`` с env при каждом
запросе, рестарт сессии не сбрасывает (их нет).
"""

from __future__ import annotations

import hmac
import os

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

#: Путь статуса (единственный открытый при включённом фильтре).
STATUS_PATH = "/api/v1/auth/status"


def enabled() -> bool:
    """Фильтр включён: задан непустой токен."""
    return bool(os.getenv("VC_AUTH_TOKEN"))


def token_ok(request) -> bool:
    """Запрос несёт верный токен (заголовок или query для медиа/скачивания)."""
    want = os.getenv("VC_AUTH_TOKEN") or ""
    if not want:
        return True
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    if auth.startswith("Bearer "):
        got = auth[len("Bearer "):]
    else:
        got = request.GET.get("token", "")
    return bool(got) and hmac.compare_digest(got, want)


@require_http_methods(["GET"])
def auth_status(request):
    """GET — включён ли фильтр (секретов не отдаёт, открыт всегда)."""
    return JsonResponse({"enabled": enabled()})


class AuthMiddleware:
    """Входная дверь: фильтр включён и токена нет — 401."""

    def __init__(self, get_response):
        self._get_response = get_response

    def __call__(self, request):
        if enabled() and request.path.startswith("/api/") \
                and request.path != STATUS_PATH and not token_ok(request):
            return JsonResponse(
                {"error": "Нужен токен доступа"}, status=401)
        return self._get_response(request)
