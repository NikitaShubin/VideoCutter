"""Автономная обвязка VideoCutter (работа с файловой системой).

Не часть «трубы» (``videocutter.core``): ядро занимается только покадровой
обработкой и ничего не хранит. Здесь — минимальный набор FS-операций,
позволяющий сервису работать автономно, и одновременно образец того, как
кастомный контур подключает ядро (Django-веб, CLI, очередь вызывают эти
функции, не зная деталей обработки).
"""

from .workspace import (
    ROLES,
    VIDEO_EXTS,
    InvalidWorkspaceError,
    WorkspaceExistsError,
    assign_role_file,
    classify_videos,
    commit_role_upload,
    create_workspace,
    create_workspace_pair,
    default_workspace_name,
    delete_workspace,
    make_upload_temp,
    promote_plain_video,
    remove_role_file,
    role_filename,
    role_of_name,
    sanitize_workspace_name,
    swap_role_files,
    workspace_path,
    write_stream,
)

__all__ = [
    "ROLES",
    "VIDEO_EXTS",
    "InvalidWorkspaceError",
    "WorkspaceExistsError",
    "assign_role_file",
    "classify_videos",
    "commit_role_upload",
    "create_workspace",
    "create_workspace_pair",
    "default_workspace_name",
    "delete_workspace",
    "make_upload_temp",
    "promote_plain_video",
    "remove_role_file",
    "role_filename",
    "role_of_name",
    "sanitize_workspace_name",
    "swap_role_files",
    "workspace_path",
    "write_stream",
]
