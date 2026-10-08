# -*- coding: utf-8 -*-
from django.urls import path

from . import backup, projects, views

urlpatterns = [
    path("workspaces", views.workspace_list, name="workspace-list"),
    path("workspaces/", views.workspace_list, name="workspace-list-slash"),
    path("workspaces/<str:workspace_id>", views.workspace_detail, name="workspace-detail"),
    path("workspaces/<str:workspace_id>/", views.workspace_detail, name="workspace-detail-slash"),
    path("workspaces/<str:workspace_id>/frame/<int:index>", views.workspace_frame, name="workspace-frame"),
    path("workspaces/<str:workspace_id>/frame/<int:index>/", views.workspace_frame, name="workspace-frame-slash"),
    path("workspaces/<str:workspace_id>/meta", views.workspace_meta, name="workspace-meta"),
    path("workspaces/<str:workspace_id>/meta/", views.workspace_meta, name="workspace-meta-slash"),
    path("workspaces/<str:workspace_id>/video/<str:role>", views.workspace_video_role, name="workspace-video-role"),
    path("workspaces/<str:workspace_id>/video/<str:role>/", views.workspace_video_role, name="workspace-video-role-slash"),
    path("workspaces/<str:workspace_id>/swap", views.workspace_swap, name="workspace-swap"),
    path("workspaces/<str:workspace_id>/swap/", views.workspace_swap, name="workspace-swap-slash"),
    path("workspaces/<str:workspace_id>/backup", backup.workspace_backup, name="workspace-backup"),
    path("workspaces/<str:workspace_id>/backup/", backup.workspace_backup, name="workspace-backup-slash"),
    path("cache", views.cache_config, name="cache-config"),
    path("cache/", views.cache_config, name="cache-config-slash"),
    path("projects", projects.project_list, name="project-list"),
    path("projects/", projects.project_list, name="project-list-slash"),
    path("projects/<str:project_id>", projects.project_detail, name="project-detail"),
    path("projects/<str:project_id>/", projects.project_detail, name="project-detail-slash"),
    path("projects/<str:project_id>/backup", backup.project_backup, name="project-backup"),
    path("projects/<str:project_id>/backup/", backup.project_backup, name="project-backup-slash"),
    path("projects/<str:project_id>/tasks", projects.project_attach, name="project-attach"),
    path("projects/<str:project_id>/tasks/", projects.project_attach, name="project-attach-slash"),
    path("projects/<str:project_id>/tasks/<str:task_id>", projects.project_detach, name="project-detach"),
    path("projects/<str:project_id>/tasks/<str:task_id>/", projects.project_detach, name="project-detach-slash"),
    path("backups/import", backup.backup_import, name="backup-import"),
    path("backups/import/", backup.backup_import, name="backup-import-slash"),
    path("uploads/<str:upload_id>/status", views.upload_status, name="upload-status"),
    path("uploads/<str:upload_id>/status/", views.upload_status, name="upload-status-slash"),
    path("debug/threads", views.debug_threads, name="debug-threads"),
]
