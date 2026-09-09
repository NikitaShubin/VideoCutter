# -*- coding: utf-8 -*-
from django.conf import settings
from django.db import models


class Fragment(models.Model):
    """Фрагмент [start, end] кадров (оба включительно) на видео-паре.

    user — пока опционально; настоящая модель прав (разметчик/админ) появится
    вместе с аутентификацией VideoDataOps.
    """

    video_pair = models.ForeignKey(
        "vc_pairs.VideoPair",
        on_delete=models.CASCADE,
        related_name="fragments",
    )
    start = models.PositiveIntegerField()
    end = models.PositiveIntegerField()
    comment = models.TextField(default="", blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marker_fragments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start", "end"]
        verbose_name = "фрагмент"
        verbose_name_plural = "фрагменты"

    def __str__(self) -> str:
        return f"[{self.start}, {self.end}]"

    def clean(self) -> None:
        if self.start > self.end:
            raise ValueError("start не может быть больше end")