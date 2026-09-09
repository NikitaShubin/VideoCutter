# -*- coding: utf-8 -*-
from django.contrib import admin

from .models import Fragment


@admin.register(Fragment)
class FragmentAdmin(admin.ModelAdmin):
    list_display = ("id", "video_pair", "start", "end", "user", "created_at")
    list_filter = ("video_pair",)