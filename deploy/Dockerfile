# One image, six roles. web-ops, web-portal and the three workers differ only
# by command and DJANGO_SETTINGS_MODULE — see docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg; curl for container healthchecks.
#
# postgresql-client-16 is for `apps/operations/backups.py`, which runs pg_dump
# and pg_restore from the worker rather than through `docker compose exec` —
# a container has no Docker socket. It comes from PGDG, not Debian: bookworm
# ships client 15, and pg_dump refuses outright to dump a newer server
# ("server version 16.x; pg_dump version 15.x"), so the Debian package would
# fail every night with an error nobody would see until a restore was needed.
# The version here must track the `pgvector/pgvector:pg16` server image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl ca-certificates gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# WeasyPrint, for PDF export (apps/outputs/exports/pdf.py). It renders through
# pango and cairo rather than shelling out to a browser, which is why these are
# libraries rather than a 400MB Chrome.
#
# fonts-dejavu-core IS NOT OPTIONAL. A slim image ships no fonts at all, and
# this content is full of curly quotes and "·" bullets — without a font that
# has them they render as empty boxes. The PDF is produced successfully and
# looks broken, which is far harder to notice than a failure.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
      libcairo2 libgdk-pixbuf-2.0-0 libffi8 \
      fonts-dejavu-core \
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
