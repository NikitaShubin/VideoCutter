# -*- coding: utf-8 -*-
from rest_framework import serializers

from .models import VideoPair


class VideoPairListSerializer(serializers.ModelSerializer):
    original_url = serializers.SerializerMethodField()

    class Meta:
        model = VideoPair
        fields = [
            "id",
            "original_name",
            "visualization_name",
            "total_frames",
            "width",
            "height",
            "fps",
            "chunk_size",
            "created_at",
            "original_url",
        ]

    def get_original_url(self, obj: VideoPair) -> str:
        return obj.original.url if obj.original else ""


class VideoPairCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = VideoPair
        fields = [
            "id",
            "original",
            "visualization",
            "original_name",
            "visualization_name",
            "total_frames",
            "width",
            "height",
            "fps",
        ]
        read_only_fields = [
            "original_name",
            "visualization_name",
            "total_frames",
            "width",
            "height",
            "fps",
        ]

    def validate(self, attrs):
        original = attrs.get("original")
        visualization = attrs.get("visualization")
        if original is None:
            raise serializers.ValidationError("Поле original обязательно")
        return attrs


class VideoPairDetailSerializer(VideoPairListSerializer):
    visualization_url = serializers.SerializerMethodField()
    fragments = serializers.SerializerMethodField()

    class Meta(VideoPairListSerializer.Meta):
        fields = VideoPairListSerializer.Meta.fields + [
            "visualization_url",
            "fragments",
        ]

    def get_visualization_url(self, obj: VideoPair) -> str:
        return obj.visualization.url if obj.visualization else obj.original.url

    def get_fragments(self, obj: VideoPair):
        from vc_fragments.models import Fragment

        qs = Fragment.objects.filter(video_pair=obj).order_by("start")
        return [{"start": f.start, "end": f.end, "comment": f.comment} for f in qs]