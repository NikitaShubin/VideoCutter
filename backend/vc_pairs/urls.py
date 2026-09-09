# -*- coding: utf-8 -*-
from rest_framework.routers import DefaultRouter

from .views import VideoPairViewSet

router = DefaultRouter()
router.register("", VideoPairViewSet, basename="pairs")

urlpatterns = router.urls