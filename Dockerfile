# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Paris

# supercronic : cron adapté aux conteneurs (logs sur stdout, pas de root requis)
ARG SUPERCRONIC_VERSION=v0.2.29
ADD https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /srv/maree

# Dépendances d'abord pour profiter du cache Docker
COPY requirements.txt .
RUN pip install -r requirements.txt

# Code applicatif (app/ + frontend statique)
COPY app/ ./app/
COPY static/ ./static/
COPY docker/crontab /etc/crontab.maree

# Utilisateur non-root ; /srv/maree/data = SQLite, /models = NetCDF FES (lecture seule)
RUN useradd --uid 1000 --create-home maree \
 && mkdir -p /srv/maree/data /models \
 && chown -R maree:maree /srv/maree
USER maree

ENV TIDE_MODEL_DIRECTORY=/models

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]