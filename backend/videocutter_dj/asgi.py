import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "videocutter_dj.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()