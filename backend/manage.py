#!/usr/bin/env python
import os
import sys

# Корень сервиса (где лежит пакет videocutter/core) — на один уровень выше backend.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ROOT = os.path.dirname(BACKEND_DIR)
if SERVICE_ROOT not in sys.path:
    sys.path.insert(0, SERVICE_ROOT)


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "videocutter_dj.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()