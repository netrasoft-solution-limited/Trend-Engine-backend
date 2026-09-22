#!/usr/bin/env bash
# End-to-end drive of the portal API over real HTTP.
#
# Asserts the properties that matter most and that a unit test cannot reach,
# because they depend on the whole stack agreeing: middleware binding,
# session cookies, CSRF, DRF permissions and the publication gate.
#
# In particular it proves the thing the gate exists for — an APPROVED output
# is not visible to the client, and a WITHDRAWN one resolves to nothing.
#
# Usage:
#   source scripts/dev-env.sh
#   DJANGO_SETTINGS_MODULE=config.settings.portal .venv/bin/python manage.py runserver 8000 &
#   .venv/bin/python manage.py seed_demo
#   scripts/smoke-portal-api.sh
set -u
B=http://127.0.0.1:8000/portal/api
pass=0; fail=0

jar() { echo "/tmp/claude-501/cj-$1.txt"; }

csrf() { # $1 = jar name
  curl -s -c "$(jar "$1")" -b "$(jar "$1")" "$B/auth/csrf" | python3 -c 'import sys,json;print(json.load(sys.stdin)["csrfToken"])'
}

post() { # $1=jar $2=path $3=json
  curl -s -o /tmp/claude-501/body.json -w "%{http_code}" \
    -c "$(jar "$1")" -b "$(jar "$1")" \
    -H "Content-Type: application/json" -H "X-CSRFToken: $(csrf "$1")" \
    -H "Referer: http://127.0.0.1:8000/" \
    -X POST "$B$2" -d "$3"
}

get() { # $1=jar $2=path
  curl -s -o /tmp/claude-501/body.json -w "%{http_code}" -c "$(jar "$1")" -b "$(jar "$1")" "$B$2"
}

body() { cat /tmp/claude-501/body.json; }

check() { # $1=label $2=expected $3=actual
  if [ "$2" = "$3" ]; then echo "  ok   $1"; pass=$((pass+1));
  else echo "  FAIL $1 — expected $2, got $3"; echo "       $(body | head -c 200)"; fail=$((fail+1)); fi
}

rm -f /tmp/claude-501/cj-*.txt

echo ""
echo "Authentication"
echo ""
check "anonymous session is 401"            401 "$(get dana /auth/session)"
check "wrong password is 401"               401 "$(post dana /auth/login '{"email":"dana@jarrow.example","password":"wrong"}')"
check "login succeeds"                      200 "$(post dana /auth/login '{"email":"dana@jarrow.example","password":"portal-demo-2026"}')"
ROLE=$(body | python3 -c 'import sys,json;print(json.load(sys.stdin)["role"])')
check "role comes from the server"          org_admin "$ROLE"
check "session now resolves"                200 "$(get dana /auth/session)"

echo ""
echo "The publication gate, over HTTP"
echo ""
get dana /publications >/dev/null
N=$(body | python3 -c 'import sys,json;print(len(json.load(sys.stdin)))')
check "exactly one live publication"        1 "$N"
TITLES=$(body | python3 -c 'import sys,json;print("|".join(p["title"] for p in json.load(sys.stdin)))')
case "$TITLES" in *Berberine*) echo "  FAIL approved-but-unpublished leaked"; fail=$((fail+1));; *) echo "  ok   approved-but-unpublished is invisible"; pass=$((pass+1));; esac
case "$TITLES" in *superseded*) echo "  FAIL withdrawn publication still visible"; fail=$((fail+1));; *) echo "  ok   withdrawn publication is invisible"; pass=$((pass+1));; esac
case "$TITLES" in *Fixture*) echo "  FAIL another tenant's content leaked"; fail=$((fail+1));; *) echo "  ok   other tenant's content is invisible"; pass=$((pass+1));; esac

echo ""
echo "Delivery tracker collapses internal states"
echo ""
get dana /deliveries >/dev/null
STATES=$(body | python3 -c 'import sys,json;print(",".join(sorted({r["state"] for r in json.load(sys.stdin)})))')
check "only client-facing states"           "in preparation,published" "$STATES"

echo ""
echo "Role enforcement is server-side"
echo ""
check "admin reaches team"                  200 "$(get dana /team)"
check "admin reaches subscription"          200 "$(get dana /subscription)"
post priya /auth/login '{"email":"priya@jarrow.example","password":"portal-demo-2026"}' >/dev/null
check "viewer is refused team"              403 "$(get priya /team)"
check "viewer is refused subscription"      403 "$(get priya /subscription)"

echo ""
echo "Multi-org switching"
echo ""
check "consultant logs in"                  200 "$(post sofia /auth/login '{"email":"sofia@consultant.example","password":"portal-demo-2026"}')"
M=$(body | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["memberships"]))')
check "sees two memberships"                2 "$M"
get sofia /publications >/dev/null
BEFORE=$(body | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["title"] if d else "none")')
FIX=$(docker exec te-postgres psql -U trend_engine -d trend_engine -tAc "select id from tenancy_organization where slug='second-client-fixture'" | tr -d ' ')
check "switch to the fixture org"           200 "$(post sofia /auth/org "{\"organization_id\":$FIX}")"
get sofia /publications >/dev/null
AFTER=$(body | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d[0]["title"] if d else "none")')
if [ "$BEFORE" != "$AFTER" ]; then echo "  ok   switching org changes the content"; pass=$((pass+1));
else echo "  FAIL content did not change on switch"; fail=$((fail+1)); fi
check "forged org id is refused"            403 "$(post sofia /auth/org '{"organization_id":999999}')"

echo ""
echo "Logout"
echo ""
check "logout succeeds"                     204 "$(post dana /auth/logout '{}')"
check "session gone after logout"           401 "$(get dana /auth/session)"

echo ""
if [ "$fail" -gt 0 ]; then echo "$fail failed, $pass passed"; exit 1; fi
echo "All $pass checks passed."
