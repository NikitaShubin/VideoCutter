# -*- coding: utf-8 -*-
from django.urls import path

from . import views

urlpatterns = [
    path("workspaces", views.workspace_list, name="workspace-list"),
    path("workspaces/", views.workspace_list, name="workspace-list-slash"),
    path("workspaces/<str:workspace_id>", views.workspace_detail, name="workspace-detail"),
    path("workspaces/<str:workspace_id>/", views.workspace_detail, name="workspace-detail-slash"),
    path("workspaces/<str:workspace_id>/frame/<int:index>", views.workspace_frame, name="workspace-frame"),
    path("workspaces/<str:workspace_id>/frame/<int:index>/", views.workspace_frame, name="workspace-frame-slash"),
    path("workspaces/<str:workspace_id>/meta", views.workspace_meta, name="workspace-meta"),
    path("workspaces/<str:workspace_id>/meta/", views.workspace_meta, name="workspace-meta-slash"),
]
