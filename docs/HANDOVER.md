# Trend Engine — handover

Current as of 21 October 2026. Everything needed to run, deploy, restore and
continue this system without the original developer. Credentials are never
written here; section 7 says where each one lives.

## 1. Repositories and the deployed commit

| Repository | Contents | Branch |
|---|---|---|
| `netrasoft-solution-limited/Trend-Engine-backend` | Django app: both planes, pipeline, screens, exports, deploy files | `main` |
| `netrasoft-solution-limited/Trend-Engine` | React client portal (built into the `frontend` image); ops screens in it are an unused mock | `main` |

The deployed commit is whatever `git -C ~/trend-engine/backend log -1` shows on
the server; every deploy is a fast-forward of `main`, so it is also the tip of
`origin/main` at the time of the last deploy.

## 2. What runs where

One DigitalOcean droplet (`geo-trend-engine-01`, `geo.omnialta.com`), Docker
Compose project `~/trend-engine/backend/deploy`:

| Service | Role |
|---|---|
| `caddy` | TLS and routing: `ops.geo.omnialta.com` → `web-ops`; `geo.omnialta.com/portal` → `web-portal` + static React build |
| `web-ops` | Operator plane (gunicorn `config.wsgi_ops`) — the app Pure Play uses |
| `web-portal` | Client portal API (gunicorn `config.wsgi_portal`) — out of scope this release, left running |
| `worker-ingest`, `worker-enrich`, `worker-default` | Celery queues `ingest` (collection), `enrich` (model calls, transcripts, detection, ranking), `default` (exports, email, backups) |
| `beat` | Celery schedule (section 5) |
| `postgres` | PostgreSQL 16 with pgvector; all data |
| `redis` | Celery broker |
| `frontend` | One-shot build of the portal assets into a volume; exiting with code 0 is normal |

## 3. Local setup

```
cd backend
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker run -d --name te-postgres -e POSTGRES_DB=trend_engine -e POSTGRES_USER=trend_engine \
  -e POSTGRES_PASSWORD=dev -p 5433:5432 pgvector/pgvector:pg16
docker run -d --name te-redis -p 6379:6379 redis:7-alpine
source scripts/dev-env.sh           # dev settings; turns the ops MFA guard off locally only
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver 8001   # operator plane (default settings)
```

Vendor keys for local runs go in `scripts/secrets.local.sh` (gitignored; never
committed). Without an `LLM_API_KEY` the model-backed steps refuse cleanly.

## 4. Deploying

From the server, always in this order:

```
cd ~/trend-engine/backend
docker exec trend-engine-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > ~/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ).dump      # 1. backup first
git pull --ff-only origin main                           # 2. code
cd deploy && docker compose build                        # 3. images
docker compose run --rm --no-deps web-ops python manage.py migrate   # 4. migrations (NOT run on start)
docker compose up -d                                     # 5. restart
```

## 5. Scheduled jobs (Celery beat, `config/celery.py`)

| Job | When (UTC) | What |
|---|---|---|
| `poll_due_sources` | every 15 min | Polls each source whose interval has elapsed |
| `assess_pending` | every 30 min | Relevance screening of new items, then transcript and extraction |
| `detect_signals` | 06:00 daily | Groups claims into developments; then ranks every client and drafts up to 3 recommendations each |
| `nightly_backup` | 03:00 daily | Off-site backup — **skipped until `BACKUP_TARGET` is set** |
| `restore_test` | Sun 04:00 | Restores the latest off-site dump into a scratch database — skipped likewise |

Interim, until a bucket exists: a server cron runs `scripts/onbox_backup.sh` at
03:00 UTC (nightly dump to `~/backups`, checked readable, 14 days kept). This is
**on-box only** and does not survive losing the server.

## 6. Data and restore

- Schema: Django migrations under `apps/*/migrations/` (36 files). `makemigrations --check`
  must report no changes under both `config.settings.ops` and `config.settings.portal`.
- Restore procedure: `deploy/RESTORE.md`.
- Restorable dumps: `~/pre-deploy-*.dump` (taken before every deploy) and `~/backups/` on the
  server. Provided on request through a secure channel, never by email.
- The audit log is append-only in code; record notes correct outputs without rewriting them.

## 7. Integrations and where credentials live

Values are never in the repository or this document.

| Integration | Used for | Credential location |
|---|---|---|
| Anthropic | relevance screening, extraction, detection, recommendations, content drafting | `LLM_API_KEY` in the server's `deploy/.env` |
| Taddy | podcast discovery and transcripts | stored encrypted in the database (Vendor keys screen); fallback `TADDY_*` in `deploy/.env` |
| Apify | YouTube collection | as Taddy (`APIFY_TOKEN`) |
| AssemblyAI | audio transcription fallback | as Taddy (`ASSEMBLYAI_API_KEY`) |
| Resend | outgoing email | `RESEND_API_KEY` in `deploy/.env` |
| Database | — | `POSTGRES_*` in `deploy/.env` |
| Encryption of stored vendor keys | — | `CREDENTIALS_ENCRYPTION_KEY` in `deploy/.env` — **must be preserved**, or stored vendor keys cannot be decrypted |
| Django | sessions, CSRF | `DJANGO_SECRET_KEY` in `deploy/.env` |

Server access is by SSH key for the `abubakar` user. Access for someone else is
arranged directly, not by email.

## 8. Tests and checks

```
lint-imports                                                    # 5 dependency contracts
python manage.py makemigrations --check --dry-run               # ops settings
python manage.py makemigrations --check --dry-run --settings=config.settings.portal
pytest --ignore=apps/portal/tests/test_registration.py --ignore=apps/portal/tests/test_deploy_settings.py
pytest apps/portal/tests/test_registration.py apps/portal/tests/test_deploy_settings.py --ds=config.settings.portal
```

All pass at the deployed commit. The AI layer is tested against a fake transport;
no test spends money or needs a key.

## 9. Known defects and unfinished work

- `README.md` (both repos) lags the code: its "not built yet" lists predate the
  connectors, signals, ranking, screens and exports. This document and the code win.
- The React repository's operator screens run on mock data and are not deployed;
  the operator app is the server-rendered Django one.
- Off-site backups and the weekly restore test wait on a storage bucket.
- No expert reviewer is registered, so nothing health-related can be signed off
  or published until Jarrow names one.
- Momentum, acceleration and engagement are not yet measurable (coverage-gated);
  scores rest on 45% of the plan's weighting.
- Not built yet: research sources (PubMed, Crossref, ClinicalTrials.gov), citation
  validation, PDP batch, non-supplement portability. The client-delivery work in
  progress is tracked in `trend-radar/mark-reply-*` and the delivery plan.
