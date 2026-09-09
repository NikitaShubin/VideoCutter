# -*- coding: utf-8 -*-
from django.urls import path

from . import views

urlpatterns = [
    path("pairs/<str:pair_id>/fragments", views.fragments, name="fragments"),
    path("pairs/<str:pair_id>/fragments/", views.fragments, name="fragments-slash"),
    path("pairs/<str:pair_id>/export", views.fragment_export, name="fragment-export"),
    path("pairs/<str:pair_id>/export/status", views.fragment_export_status, name="fragment-export-status"),
    path(
        "pairs/<str:pair_id>/export/<path:path>",
        views.fragment_export_download,
        name="fragment-export-download",
    ),
]
