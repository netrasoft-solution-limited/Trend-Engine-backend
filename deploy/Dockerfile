# One image, six roles. web-ops, web-portal and the three workers differ only
# by command and DJANGO_SETTINGS_MODULE — see docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg; curl for container healthchecks.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY . .

# Non-root. The app never needs to write outside the media volumes.
RUN useradd --system --create-home --uid 10001 trend \
 && chown -R trend:trend /app
USER trend

EXPOSE 8000
