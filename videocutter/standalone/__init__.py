"""Автономная обвязка VideoCutter (работа с файловой системой).

Не часть «трубы» (``videocutter.core``): ядро занимается только покадровой
обработкой и ничего не хранит. Здесь — минимальный набор FS-операций,
позволяющий сервису работать автономно, и одновременно образец того, как
кастомный контур подключает ядро (Django-веб, CLI, очередь вызывают эти
функции, не зная деталей обработки).
"""

from .workspace import (
    VIDEO_EXTS,
    InvalidWorkspaceError,
    WorkspaceExistsError,
    create_workspace,
    default_workspace_name,
    delete_workspace,
    sanitize_workspace_name,
    workspace_path,
)

__all__ = [
    "VIDEO_EXTS",
    "InvalidWorkspaceError",
    "WorkspaceExistsError",
    "create_workspace",
    "default_workspace_name",
    "delete_workspace",
    "sanitize_workspace_name",
    "workspace_path",
]
