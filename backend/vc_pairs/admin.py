# -*- coding: utf-8 -*-
from django.contrib import admin

from .models import VideoPair


@admin.register(VideoPair)
class VideoPairAdmin(admin.ModelAdmin):
    list_display = ("id", "original_name", "total_frames", "width", "height", "fps", "created_at")
    search_fields = ("original_name",)