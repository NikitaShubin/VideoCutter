from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("vc_pairs.urls")),
    path("api/v1/", include("vc_fragments.urls")),
]

if settings.DEBUG:
    # В dev-режиме отдаём статику и медиа (workspace files).
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
