# Trend Engine — backend

A Django modular monolith, two authentication realms, one database.

The **tenant plane** is built and working: real accounts, sessions, invites,
multi-org membership, and the publication gate. The **operator plane** boots and
owns its models but has no UI yet, and the evidence pipeline (L1–L4) is still
the original skeleton.

Derived from `../trend-engine-prd.md` and `../trend-engine-solution-architecture.md`.
Those documents win any disagreement with this one.

> **Build status.** PRD §16 requires Mark's sign-off on scope, timeline, budget
> and commercial model before building on this basis. One thing is marked
> non-negotiable regardless of how Phase 7 is sequenced (Arch §16): the
> tenant-aware schema and the `Organization` model land in Phase 1. *"The portal
> can wait, the schema cannot."* That is what this skeleton is sized for.

## Layout

```
backend/
├── manage.py                 defaults to the OPERATOR settings
├── pyproject.toml
├── .importlinter             the dependency contracts — see below
├── conftest.py               shared tenancy fixtures
├── config/
│   ├── celery.py             three queues: ingest · enrich · default
│   ├── settings/
│   │   ├── base.py           shared; installs NO auth or tenant middleware
│   │   ├── ops.py            operator plane
│   │   └── portal.py         tenant plane
│   ├── urls_ops.py           nine operator screens
│   ├── urls_portal.py        one include, deliberately
│   ├── wsgi_ops.py           ─┐ two processes,
│   └── wsgi_portal.py        ─┘ one codebase
├── apps/                     sixteen apps, ordered by layer
├── templates/{ops,portal}/   shared chrome; per-screen templates live in apps
├── tests/
│   ├── tenancy/              the three deployment-blocking suites
│   ├── publication/          the gate is the only door
│   └── fixtures/             second-client + non-supplement domain (PRD §2)
├── deploy/                   Dockerfile · docker-compose.yml · Caddyfile
└── scripts/                  backup.sh · restore_test.sh
```

## The apps

Ordered by layer, lowest first. **The L2/L5 boundary is "the single most
important line in the system"** (Arch §3): below it, evidence is shared across
every client, which is why client #2 costs a fraction of client #1.

| App | Layer | Owns | Tenant |
|---|---|---|---|
| `tenancy` | L0 | `Organization`, default-deny managers, plane middleware | — |
| `sources` | L1 | Provider registry, policy versions, source config | No |
| `connectors` | L1 | Adapter implementations | No |
| `ingestion` | L1–L2 | Run orchestration, raw items, normalization | No |
| `evidence` | L2 | Content items, segments, dedup, rights, deletion | No |
| `domains` | L3 | Domain packs, taxonomy, compliance rules | No |
| `enrichment` | L3–L4 | Extraction, entities, claims, embeddings | No |
| `research` | L3–L4 | Research records, integrity, assessments | No |
| `intelligence` | L4 | Clustering, signals, component scores, confidence | No |
| `clients` | L5 | Client profiles, assets, voice, reviewers | **Yes** |
| `scoring` | L5 | Tenant relevance, recommendations, feedback | **Yes** |
| `outputs` | L6 | Builder, versions, approvals, claims validation | **Yes** |
| `publication` | L6–L7 | **The gate** | **Yes** |
| `portal` | L7 | Client-facing views, org users, notifications | **Yes** |
| `billing` | L7 | Subscriptions, plans, invoices, processor refs | **Yes** |
| `operations` | cross | Health, cost ledger, dead letters, audit log | No |

Screens from PRD §6.5 are owned by the app that owns their data: Triage and
Signal Review by `scoring`, Ingestion Runs by `ingestion`, Resolution Queue by
`evidence`, Organizations by `billing`, and so on. For the operator plane,
`../frontend/src/ops` is the design reference those templates get built from —
ADR #9 stands there. For the tenant plane it does not: `../frontend/src/portal`
is a real client against `apps/portal`'s JSON API (Arch §15.1 A1).

## Deviations from the architecture, flagged on purpose

**`Organization` lives in `tenancy` (L0), not `billing` (L7).** Every
tenant-scoped model from L5 up carries a foreign key to the tenant root, so
keeping that root at L7 would invert the dependency rule — `clients` would have
to import from `billing`. `billing` keeps Subscription, Plan, Invoice and
PaymentRecord, all pointing down at it. PRD §6.8 is unaffected: Organization
still maps 1:1 to Client, additively.

**Tenancy machinery lives in its own app, not in `clients`.** Arch §5.2 sketches
`clients/managers.py` and labels it "conceptual shape". Six apps need the
manager; putting it in one of them would make the other five import sideways.

**`OrgUser`, `OrgMembership`, `OrgInvite` and `EmailVerificationToken` are NOT
tenant-scoped.** They cannot be. `AuthenticationMiddleware` resolves `request.user` through the
default manager *before* any tenant is bound — binding needs the user, and the
user needs the query — so a scoped manager here deadlocks every authenticated
request. `PortalBackend.authenticate()` has the same problem: it finds a user by
email before any organisation is known. Do not "fix" this; it will take the
portal down on the first login.

**The `__Host-` cookie prefix forced the plane split.** Arch §5.3 paired
`__Host-te_ops` with `Path=/ops`, which no browser accepts — the prefix requires
`Path=/`. The planes now differ by origin instead (`ops.<domain>`), which is a
stronger boundary anyway. Recorded as Arch §15.1 A2.

**Self-service organisation registration overrules PRD §6.8's invite-only
rule — deliberately.** A new organisation can register itself at
`POST /portal/api/auth/register`, and it becomes `ACTIVE` as soon as its admin
verifies their email. **No operator approval is involved.** The first admin's
membership is `PENDING_VERIFICATION` until then, and that membership status is
what blocks login. The organisation's `ONBOARDING` status does not block it,
because `active_memberships()` lets ONBOARDING organisations in. The registrant
is always `ORG_ADMIN`; the request has no way to set a role. Joining an
*existing* organisation is still invite-only. An email that already has an
account cannot register a second organisation; that account gets the same
generic response as anyone else. It is off unless `PORTAL_ALLOW_SELF_SIGNUP=1`
is set, and when off the register endpoint answers 404. Registration and
verification are recorded as `PortalLoginEvent` outcomes (`registered`,
`verified`, `verify_failed`) rather than `AuditEvent`s, because the portal may
not import `apps.operations`. Not yet addressed:
- An unverified registration holds its email address and slug indefinitely.
  There is no cleanup job.
- Registration does not notify operators.

All of these are open to being overruled — they are recorded rather than buried,
here and in the architecture document's amendments table.

## The contracts

`lint-imports` enforces five contracts in `.importlinter`. They block
deployment (Arch §5.4), because they are how the tenancy tests start passing
for the wrong reason:

- **Layered architecture** — dependencies point downward only.
- **Evidence stays global** — L1–L4 never import `clients`, `scoring`,
  `outputs`, `publication`, `portal` or `billing`.
- **The gate is the only door** — `portal` never imports `outputs` (Arch §9.3).
- **The portal sees nothing internal** — `portal` cannot import evidence,
  scoring or cost modules at all, so a template cannot render what its module
  cannot import (PRD §3.2).
- **Tenancy depends on nothing** — otherwise a cycle gets resolved by weakening
  the manager, which is the one thing that must not happen.

`operations` is excluded from the layers contract and constrained by the
explicit ones. It is genuinely cross-cutting: higher layers write to its audit
log and cost ledger while its dashboard reads health from lower ones.

## The two things most likely to be "simplified"

**Default-deny at the ORM layer** (`apps/tenancy/`). `TenantScopedManager`
raises `TenantScopeError` when nothing is bound. Operator access is an explicit
`OPERATOR_ALL` opt-out, and the portal plane marks itself on every request so
that binding it *raises*. Arch §5.2: "a developer who forgets scoping gets a
loud failure in development, not a data leak in production." A
`TenantScopeError` in production is a P1 (Arch §13).

**Approval is not publication** (`apps/publication/services.py`). Approving
signs off on an exact version; publishing makes it visible and notifies the
organisation. An operator may legitimately approve ahead of a delivery date or
for a reviewer packet. Collapsing the states makes accidental disclosure one
mis-click, and the manual email-forwarding step that used to catch mistakes is
gone. Health and scientific outputs need a recorded expert sign-off *before*
publication — stricter than approval, because publication is what reaches the
client.

## Running it locally

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker run -d --name te-postgres -e POSTGRES_DB=trend_engine \
  -e POSTGRES_USER=trend_engine -e POSTGRES_PASSWORD=dev \
  -p 5433:5432 pgvector/pgvector:pg16
docker run -d --name te-redis -p 6379:6379 redis:7-alpine

source scripts/dev-env.sh
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo          # prints the demo accounts

DJANGO_SETTINGS_MODULE=config.settings.portal .venv/bin/python manage.py runserver 8000
scripts/smoke-portal-api.sh                   # checks over real HTTP (SERVER_LOG=... for the verify leg)
```

The React portal in `../frontend` proxies `/portal/api` to port 8000, so
`npm run dev` there gives you the whole stack.

## Running it as deployed

```
cp deploy/.env.example deploy/.env    # fill it in
docker compose -f deploy/docker-compose.yml up --build
```

Nine services on one VPS (Arch §11.1). Object storage and the nightly dump live
off-box; the acquisition vendors are external. No Kubernetes, no managed
services, no microservices — all three are explicit anti-goals, and §11.2 gives
the cost reason.

`scripts/restore_test.sh` runs weekly against a disposable database and asserts
the restored data is actually there — a dump of an empty database restores
perfectly. Arch §11.3 makes this an acceptance criterion, not a nicety.

## Checks

```
.venv/bin/python manage.py makemigrations --check --dry-run                                  # ops
.venv/bin/python manage.py makemigrations --check --dry-run --settings=config.settings.portal
lint-imports                          # the five dependency contracts
pytest apps/portal/tests/test_registration.py apps/portal/tests/test_deploy_settings.py --ds=config.settings.portal
SERVER_LOG=<runserver log> scripts/smoke-portal-api.sh   # checks over real HTTP
```

`.github/workflows/ci.yml` runs the first four on every push and pull request.
Its manual `workflow_dispatch` run generates any missing migrations instead and
uploads them as the `generated-migrations` artifact.

**The dual `makemigrations --check` is the important one.** Two settings modules
declare different `AUTH_USER_MODEL` values over one migration history. That is
safe only while no migration contains a `SettingsReference` — otherwise the same
foreign key resolves to `operations_operatoruser` in one process and
`portal_orguser` in the other, silently joining two identity tables on the same
integer key. If both commands report "No changes detected", the migration set is
provably settings-independent. Treat it as deployment-blocking, like
`lint-imports`.

## What is built

- **The tenant realm.** `OrgUser`, `OrgMembership` (multi-org: the role lives on
  the membership), `OrgInvite`, `PortalLoginEvent`, and a `PortalBackend`.
- **The auth API** under `/portal/api/` — login, logout, session, org switch,
  password reset, invite acceptance, team management. Session cookies, not
  tokens. Rate-limited login, and both successes and failures audited.
- **The publication gate**, for real. `publish` refuses an unapproved version
  and refuses health/scientific content without a recorded expert sign-off;
  `unpublish` is reversible and audited; a partial unique index enforces "one
  live publication per output per tenant" in the database rather than in
  application code that could race.
- **The content API** — publications, deliveries, notifications, subscription.
- **Self-service registration** (flag-gated; see the deviation above):
  `auth/register`, `auth/verify` and `auth/verify/resend`. Verification tokens
  are single-use, expire after 24 hours and are stored only as a hash. Both
  register and resend are throttled per email and per IP, and both answer
  identically whether or not the address is known.

## What is not here yet

- **The operator plane has no UI.** `config/urls_ops.py` includes app URLconfs
  that are still empty. `manage.py` and the tests run under ops settings, so the
  models and admin work; there are no operator views.
- **The evidence pipeline** — L1 to L4 — is still the original skeleton. No
  connectors, no ingestion, no scoring.
- **`conftest.py` fixtures still raise `NotImplementedError`,** so the tenancy
  suites read as specifications and do not yet assert. `scripts/smoke-portal-api.sh`
  covers the same ground over HTTP in the meantime.
- **CI covers only part of the blocking set.** `.github/workflows/ci.yml` runs
  the contracts, the dual `makemigrations --check` and the portal tests (registration, deployment settings).
  The tenancy and publication suites under `tests/` do not collect yet, so they
  are not run.
