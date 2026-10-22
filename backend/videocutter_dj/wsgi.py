import os

import priority
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "videocutter_dj.settings")
# Тяжёлое (декод, индексация, экспорт) — пониженным приоритетом: система
# получает процессор первой, свободные ядра достаются сервису.
priority.apply_low_priority()
application = get_wsgi_application()
