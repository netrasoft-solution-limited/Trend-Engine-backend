# Local development environment. Source it: `source scripts/dev-env.sh`
#
# These are NOT secrets. The key below is a throwaway for local development and
# is committed deliberately so a new machine can run `manage.py` immediately.
# Production values live in deploy/.env, which is gitignored (PRD §7.1:
# secrets outside source control, separate production and development
# credentials).

export DJANGO_SECRET_KEY="dev-only-not-a-secret-$(whoami)"
export DJANGO_DEBUG=1
export DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
export OPS_ALLOWED_HOSTS="ops.localhost,localhost,127.0.0.1"
export PORTAL_ALLOWED_HOSTS="localhost,127.0.0.1"
export PORTAL_TRUSTED_ORIGINS="http://localhost:5173,http://localhost:8000"
export OPS_TRUSTED_ORIGINS="http://ops.localhost:8001"

export POSTGRES_DB=trend_engine
export POSTGRES_USER=trend_engine
export POSTGRES_PASSWORD=dev
export POSTGRES_HOST=127.0.0.1
export POSTGRES_PORT=5433

export REDIS_URL="redis://127.0.0.1:6379/0"

# Arch §10.2's ceiling, enforced before every model call. Low on purpose for
# development: a runaway loop should stop within pennies, not within the
# production budget. Unset means unmetered, and the guard says so loudly.
export LLM_MONTHLY_CAP_USD=5.00
export LLM_PER_CALL_MAX_USD=0.25

# Self-service organisation registration. Off by default in code and in
# production (deploy/.env.example); on here so the smoke script can drive it.
export PORTAL_ALLOW_SELF_SIGNUP=1

# Ops login without a second factor, which is not built yet. Honoured only with
# DJANGO_DEBUG=1; production refuses ops login outright (config/settings/ops.py).
export OPS_PASSWORD_ONLY_LOGIN=1

# Where emailed links point: invitations, password resets and email
# verification are all built from this. The Vite dev server for ../frontend.
export PORTAL_PUBLIC_URL="http://localhost:5173"
export LOG_LEVEL=INFO

# Cookies are Secure in base.py, which a plain-HTTP dev server cannot set.
# This switch lets local development work without weakening the defaults for
# anyone who forgets to set it.
export DJANGO_INSECURE_COOKIES=1

# Live vendor keys, if present. Gitignored — see scripts/secrets.local.sh.
# Absent on a fresh clone, which is why every connector must degrade to a clear
# "no credentials" error rather than a confusing failure deep in a request.
[ -f "$(dirname "${BASH_SOURCE[0]:-scripts/dev-env.sh}")/secrets.local.sh" ] \
  && source "$(dirname "${BASH_SOURCE[0]:-scripts/dev-env.sh}")/secrets.local.sh"
