# Trend Engine — backend

A Django modular monolith. Structure only: directories, app configs, and real
content in the files that carry an architectural rule. Models, views and
migrations are deliberately empty — this is the skeleton the build fills in.

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
`evidence`, Organizations by `billing`, and so on. `../frontend` is the design
reference these templates get built from — ADR #9 rejects an SPA, so it is not
a deployed artifact.

## Two deviations from Arch §4, flagged on purpose

**`Organization` lives in `tenancy` (L0), not `billing` (L7).** Every
tenant-scoped model from L5 up carries a foreign key to the tenant root, so
keeping that root at L7 would invert the dependency rule — `clients` would have
to import from `billing`. `billing` keeps Subscription, Plan, Invoice and
PaymentRecord, all pointing down at it. PRD §6.8 is unaffected: Organization
still maps 1:1 to Client, additively.

**Tenancy machinery lives in its own app, not in `clients`.** Arch §5.2 sketches
`clients/managers.py` and labels it "conceptual shape". Six apps need the
manager; putting it in one of them would make the other five import sideways.

Both are open to being overruled — they are recorded here rather than buried.

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

## Running it

```
cp deploy/.env.example deploy/.env    # fill it in
docker compose -f deploy/docker-compose.yml up --build
```

Nine services on one VPS (Arch §11.1). Object storage and the nightly dump live
off-box; the acquisition vendors are external. No Kubernetes, no managed
services, no microservices — all three are explicit anti-goals, and §11.2 gives
the cost reason.

```
pytest -m tenancy          # the three blocking suites
pytest -m publication      # the gate
lint-imports               # the five contracts
```

`scripts/restore_test.sh` runs weekly against a disposable database and asserts
the restored data is actually there — a dump of an empty database restores
perfectly. Arch §11.3 makes this an acceptance criterion, not a nicety.

## What is not here

- **No models.** `models.py` in each app carries its PRD §8 group as a
  docstring and nothing else.
- **No migrations.** The first one must include the tenant-aware schema and
  `Organization` (Arch §16).
- **No views or templates.** `../frontend` is the reference for all fifteen
  screens.
- **`conftest.py` fixtures raise `NotImplementedError`** so the test suites read
  as specifications now and fail loudly rather than passing vacuously.
- **No CI workflow.** The contracts and the four blocking suites need one before
  any of this is a real gate.
