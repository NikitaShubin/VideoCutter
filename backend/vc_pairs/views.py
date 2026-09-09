# -*- coding: utf-8 -*-
from django.http import HttpResponse, Http404, StreamingHttpResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from . import frame_provider
from .models import VideoPair
from .serializers import (
    VideoPairCreateSerializer,
    VideoPairListSerializer,
    VideoPairDetailSerializer,
)


class VideoPairViewSet(viewsets.ModelViewSet):
    queryset = VideoPair.objects.all().order_by("-id")
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return VideoPairCreateSerializer
        if self.action == "retrieve":
            return VideoPairDetailSerializer
        return VideoPairListSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = serializer.save()

        original_path = obj.original.path
        try:
            meta = frame_provider.get_metadata(original_path)
        except ValueError as e:
            obj.delete()
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        obj.total_frames = meta["total_frames"]
        obj.width = meta["width"]
        obj.height = meta["height"]
        obj.fps = meta["fps"]
        obj.original_name = obj.original.name.split("/")[-1]
        if obj.visualization:
            v_meta = frame_provider.get_metadata(obj.visualization.path)
            obj.visualization_name = obj.visualization.name.split("/")[-1]
            if v_meta["total_frames"] != obj.total_frames:
                obj.delete()
                return Response(
                    {
                        "error": (
                            "Число кадров исходника и визуализации различается: "
                            f"{obj.total_frames} vs {v_meta['total_frames']}"
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            obj.visualization_name = ""
        obj.save()

        return Response(
            VideoPairDetailSerializer(obj).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="frame/(?P<index>[0-9]+)")
    def frame(self, request, pk=None, index=None):
        """Возвращает JPEG кадра по индексу (0-based)."""
        obj = self.get_object()
        index = int(index)
        video = request.query_params.get("video", "visualization")

        if video == "original" and obj.original:
            path = obj.original.path
        else:
            # Просмотр идёт по визуализации; если её нет — по оригиналу.
            path = (obj.visualization or obj.original).path

        jpeg, mime = frame_provider.get_frame_jpeg(path, index)
        if jpeg is None:
            raise Http404(f"Кадр {index} недоступен (всего кадров: {obj.total_frames})")
        return HttpResponse(jpeg, content_type=mime)

    @action(detail=True, methods=["get"], url_path="meta")
    def meta(self, request, pk=None):
        """Метаданные пары (в т.ч. уже сохранённые)."""
        obj = self.get_object()
        return Response(VideoPairDetailSerializer(obj).data)