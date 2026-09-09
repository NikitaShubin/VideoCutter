# -*- coding: utf-8 -*-
from rest_framework import serializers

from .models import Fragment


class FragmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Fragment
        fields = ["id", "video_pair", "start", "end", "comment", "user", "created_at", "updated_at"]
        read_only_fields = ["id", "user", "created_at", "updated_at"]

    def validate(self, attrs):
        start = attrs.get("start")
        end = attrs.get("end")
        if start is None or end is None:
            raise serializers.ValidationError("Необходимы start и end")
        if start > end:
            raise serializers.ValidationError("start не может быть больше end")
        return attrs