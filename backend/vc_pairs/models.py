# -*- coding: utf-8 -*-
import os

from django.conf import settings
from django.db import models


def _pair_path(instance: "VideoPair", filename: str) -> str:
    pair_id = instance.pk or "new"
    return os.path.join("pairs", pair_id, filename)


class VideoPair(models.Model):
    """Пара видео: оригинал + визуализация работы нейросети.

    Два файла с одинаковым числом кадров. Обычно пользователь смотрит
    визуализацию (visualization), а вырезаются фрагменты оригинала (original).
    """

    original = models.FileField(upload_to=_pair_path)
    visualization = models.FileField(upload_to=_pair_path, null=True, blank=True)

    original_name = models.CharField(max_length=255, blank=True)
    visualization_name = models.CharField(max_length=255, blank=True)

    # Метаданные исходного видео (из оригинального файла).
    total_frames = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    fps = models.FloatField(default=0.0)

    # Общий параметр размера «чанка» (кадров на фиксируемое окно при покадровом показе).
    chunk_size = models.PositiveIntegerField(default=settings.VC_CHUNK_SIZE)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "видео-пара"
        verbose_name_plural = "видео-пары"

    def __str__(self) -> str:
        return self.original_name or f"pair-{self.pk}"

    @property
    def visualization_is_set(self) -> bool:
        return bool(self.visualization_name)