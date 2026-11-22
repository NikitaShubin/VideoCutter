# -*- coding: utf-8 -*-
from django.urls import path

from . import views

urlpatterns = [
    # Статические пути — до <job_id>, иначе "types" станет id.
    path("jobs/types", views.job_types, name="job-types"),
    path("jobs/types/", views.job_types, name="job-types-slash"),
    path("jobs/summary", views.jobs_summary, name="jobs-summary"),
    path("jobs/summary/", views.jobs_summary, name="jobs-summary-slash"),
    path("jobs", views.jobs_collection, name="jobs"),
    path("jobs/", views.jobs_collection, name="jobs-slash"),
    path("jobs/<str:job_id>", views.job_detail, name="job-detail"),
    path("jobs/<str:job_id>/", views.job_detail, name="job-detail-slash"),
    path("jobs/<str:job_id>/cancel", views.job_cancel, name="job-cancel"),
    path("jobs/<str:job_id>/cancel/", views.job_cancel, name="job-cancel-slash"),
    path("jobs/<str:job_id>/retry", views.job_retry, name="job-retry"),
    path("jobs/<str:job_id>/retry/", views.job_retry, name="job-retry-slash"),
    path("jobs/<str:job_id>/download", views.job_download, name="job-download"),
    path("jobs/<str:job_id>/download/", views.job_download, name="job-download-slash"),
]
