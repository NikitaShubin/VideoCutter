# -*- coding: utf-8 -*-
"""Регистрация встроенных типов операций.

Этап 1 каркаса: типы добавляются по мере переноса экспорта, бэкапов и
системных задач. Планировщик и API от их количества не зависят.
"""

from __future__ import annotations

from .registry import JobRegistry


def register_builtin(registry: JobRegistry) -> None:
    """Точка расширения: здесь регистрируются типы VideoCutter."""
    from . import (builtin_archive, builtin_export, builtin_import,
                   builtin_system)

    builtin_export.register(
        registry, prerequisites=builtin_system.validation_prerequisites)
    builtin_archive.register(registry)
    builtin_import.register(registry)
    builtin_system.register(registry)
