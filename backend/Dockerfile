FROM python:3.12-slim

ENV TERM=xterm \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg (экспорт фрагментов) + системные библиотеки для OpenCV/PyAV.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/videocutter

# Сначала копируем только описание пакета и ядро — пересборка при изменениях только в них.
COPY pyproject.toml ./
COPY videocutter/ videocutter/
RUN pip install --no-cache-dir .

# Затем — код веб-обвязки (backend). Web-зависимости (Django/DRF) берём из requirements.
COPY backend/ backend/
RUN pip install --no-cache-dir -r backend/requirements.txt
WORKDIR /srv/videocutter/backend

EXPOSE 8001

# Прод: gthread, workers=1 (общий in-memory GOP-кэш!), потоки — env.
# Локальная отладка: docker compose run --rm videocutter_backend
# python manage.py runserver 0.0.0.0:8001
CMD ["sh", "-c", "gunicorn videocutter_dj.wsgi:application --workers 1 --worker-class gthread --threads ${VC_WEB_THREADS:-32} --bind 0.0.0.0:8001 --timeout ${VC_WEB_TIMEOUT:-120}"]
