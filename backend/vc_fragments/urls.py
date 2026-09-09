# -*- coding: utf-8 -*-
from django.urls import path

from . import views

urlpatterns = [
    path("pairs/<int:pair_id>/fragments", views.fragment_list, name="fragments"),
    path("pairs/<int:pair_id>/fragments/", views.fragment_list, name="fragments-slash"),
    path("pairs/<int:pair_id>/fragments/replace", views.fragment_replace, name="fragments-replace"),
    path("pairs/<int:pair_id>/fragments/<int:frag_id>", views.fragment_delete, name="fragment-delete"),
    path("pairs/<int:pair_id>/export", views.fragment_export, name="fragment-export"),
    path("pairs/<int:pair_id>/export/status", views.fragment_export_status, name="fragment-export-status"),
    path(
        "pairs/<int:pair_id>/export/<path:path>",
        views.fragment_export_download,
        name="fragment-export-download",
    ),
]