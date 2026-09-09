# -*- coding: utf-8 -*-
"""Settings для stateless VideoCutter.

Никакой БД — workspace-ы на диске, фрагменты в TSV.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "video-cutter-dev-secret-key")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "corsheaders",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "videocutter_dj.urls"

WSGI_APPLICATION = "videocutter_dj.wsgi.application"

# Stateless: никакой БД.
DATABASES = {}

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

# Workspace root: корневая директория со всеми workspace-ами.
VC_WORKSPACE_ROOT = os.getenv(
    "VC_WORKSPACE_ROOT", os.path.join(BASE_DIR, "workspaces")
)

# Chunk size (для фронтенда, если нужен).
VC_CHUNK_SIZE = int(os.getenv("VC_CHUNK_SIZE", "36"))

# CORS: для dev-режима разрешаем фронт (localhost:3001).
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3001,http://localhost:5173").split(",")
    if o.strip()
]
