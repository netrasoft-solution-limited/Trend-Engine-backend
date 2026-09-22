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
export LOG_LEVEL=INFO

# Cookies are Secure in base.py, which a plain-HTTP dev server cannot set.
# This switch lets local development work without weakening the defaults for
# anyone who forgets to set it.
export DJANGO_INSECURE_COOKIES=1
