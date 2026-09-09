# -*- coding: utf-8 -*-
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "video-cutter-dev-secret-key")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "vc_pairs",
    "vc_fragments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "videocutter_dj.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "videocutter_dj.wsgi.application"

# База данных: по умолчанию SQLite (для локальной разработки), в docker — PostgreSQL.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql"
        if os.getenv("VC_DB_ENGINE", "sqlite") == "postgres"
        else "django.db.backends.sqlite3",
        "NAME": os.getenv("VC_DB_NAME", os.path.join(BASE_DIR, "db.sqlite3")),
        "USER": os.getenv("VC_DB_USER", ""),
        "PASSWORD": os.getenv("VC_DB_PASSWORD", ""),
        "HOST": os.getenv("VC_DB_HOST", ""),
        "PORT": os.getenv("VC_DB_PORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.getenv("VC_STATIC_ROOT", os.path.join(BASE_DIR, "static"))

# Медиа (загруженные видео) храним на сервере в отдельном каталоге.
MEDIA_URL = "/media/"
MEDIA_ROOT = os.getenv(
    "VC_MEDIA_ROOT", os.path.join(BASE_DIR, "vc_media")
)
# Каталог для экспортированных фрагментов (не отдаётся напрямую, только через API).
VC_EXPORT_ROOT = os.getenv("VC_EXPORT_ROOT", os.path.join(MEDIA_ROOT, "exports"))

# Номер кадра и кэш.
VC_CHUNK_SIZE = int(os.getenv("VC_CHUNK_SIZE", "36"))

# CORS: для dev-режима разрешаем фронт (localhost:3000) и любой на 8000.
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if o.strip()
]

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}