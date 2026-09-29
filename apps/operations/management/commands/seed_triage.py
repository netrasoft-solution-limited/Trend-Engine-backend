"""Seed the Triage home's data to match the ops frontend mock.

The mock (`src/data/signals.ts` in the frontend) shows, for the collection
window Sep 13 – Sep 20, 2026:

    12 new candidates · 4 review ready · 1 research alert · 0 blocking failures

with SIG-2041 ranked first. The six signals the mock describes in full are
seeded with its ids, titles and scores. The mock's counts need more signals
than it describes, so the rest are fillers, marked as such below.

Deviation from the mock: every candidate's `first_seen_at` falls inside the
window, because "new" means first seen after the previous digest. The mock's
"first seen 11 days ago" for SIG-2041 would put it before the window.

Idempotent: safe to re-run. Runs under operator scope because client scores
are tenant-scoped rows written for more than one tenant.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.ingestion.models import IngestionRun
from apps.intelligence.models import Digest, Signal
from apps.scoring.models import ClientSignalScore
from apps.sources.models import Source
from apps.tenancy.context import operator_scope
from apps.tenancy.models import Organization

#: When each digest was produced: the previous one closes the window's start.
PREVIOUS_DIGEST_AT = datetime(2026, 9, 13, 6, 0, tzinfo=UTC)
CURRENT_DIGEST_AT = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)

S = Signal.State

# (code, title, state, domain_score, jarrow fit, confidence, flags)
# flags: "review" = awaiting_operator_judgment, "research" = needs_scientific_routing
SIGNALS = [
    # ── From the mock ───────────────────────────────────────────────────────
    ("SIG-2041", "Creatine reframed as a cognition / healthy-aging supplement",
     S.CANDIDATE, 82, 82, 74, ()),
    ("SIG-2038", "Magnesium form comparison (glycinate vs. threonate) dominating sleep talk",
     S.IN_REVIEW, 74, 82, 69, ("review",)),
    ("SIG-2035", 'Fermented / postbiotic framing replacing "probiotic CFU count" talk',
     S.WATCHING, 63, 73, 55, ()),
    ("SIG-2032", "Berberine \"nature's Ozempic\" framing resurging with a safety backlash",
     S.CANDIDATE, 74, 55, 81, ("research",)),
    ("SIG-2029", "Protein target inflation (1g per lb) reaching non-athlete audiences",
     S.CANDIDATE, 64, 45, 66, ()),
    ("SIG-2026", "Vitamin D + K2 co-dosing questions spiking in Q4 immunity content",
     S.CANDIDATE, 61, 69, 62, ()),
    # The mock's audit log records SIG-2031 as rejected (duplicate topic).
    ("SIG-2031", "Collagen peptides for joint recovery (duplicate topic)",
     S.REJECTED, 52, 40, 58, ()),
    # ── Fillers, so the counts match the mock's summary ─────────────────────
    ("SIG-2044", "Electrolyte mixes marketed for cognitive focus", S.CANDIDATE, 58, 50, 61, ()),
    ("SIG-2045", "Ashwagandha cycling protocols in stress content", S.CANDIDATE, 57, 63, 59, ()),
    ("SIG-2046", "Glycine before bed as a budget sleep stack", S.CANDIDATE, 55, 58, 57, ()),
    ("SIG-2047", "Taurine longevity claims after mouse-study coverage",
     S.CANDIDATE, 54, 49, 52, ()),
    ("SIG-2048", "Omega-3 index testing moving direct-to-consumer", S.CANDIDATE, 51, 44, 63, ()),
    ("SIG-2049", "Beetroot nitrate for non-athlete energy", S.CANDIDATE, 48, 41, 55, ()),
    ("SIG-2050", "Tart cherry as a melatonin alternative", S.CANDIDATE, 46, 52, 50, ()),
    ("SIG-2051", "L-theanine paired with caffeine in 'calm focus' content",
     S.CANDIDATE, 44, 47, 54, ()),
    ("SIG-2036", "Zinc carnosine for gut-lining claims", S.IN_REVIEW, 60, 57, 64, ("review",)),
    ("SIG-2034", "NMN versus NR consumer confusion", S.IN_REVIEW, 59, 48, 60, ("review",)),
    ("SIG-2030", "Iron bisglycinate for fatigue in women's health content",
     S.IN_REVIEW, 56, 66, 62, ("review",)),
]

#: The mock scores SIG-2041 for the second tenant too, at a lower fit.
FIXTURE_SCORES = [("SIG-2041", 59, 74)]

# (name, route, status, last success)
SOURCES = [
    ("Huberman Lab (RSS)", Source.Route.PODCAST, Source.Status.ACTIVE,
     CURRENT_DIGEST_AT - timedelta(minutes=18)),
    ("Examine / clinician YouTube set", Source.Route.YOUTUBE, Source.Status.DEGRADED,
     CURRENT_DIGEST_AT - timedelta(minutes=52)),
    ("PubMed / PMC — supplement MeSH set", Source.Route.RESEARCH, Source.Status.ACTIVE,
     CURRENT_DIGEST_AT - timedelta(hours=2)),
]

# (source name, started, minutes taken, status, blocking)
RUNS = [
    ("Huberman Lab (RSS)", datetime(2026, 9, 20, 9, 40, tzinfo=UTC), 3,
     IngestionRun.Status.SUCCEEDED, False),
    # The mock's "partial" run: two items stored metadata-only. It failed, but
    # the next run retries it, so it does not block — and must not count.
    ("Examine / clinician YouTube set", datetime(2026, 9, 20, 9, 12, tzinfo=UTC), 12,
     IngestionRun.Status.FAILED, False),
    ("PubMed / PMC — supplement MeSH set", datetime(2026, 9, 20, 8, 5, tzinfo=UTC), 6,
     IngestionRun.Status.SUCCEEDED, False),
]


class Command(BaseCommand):
    help = "Seed signals, client scores, sources, runs and digests for the Triage home."

    @transaction.atomic
    def handle(self, *args, **options):
        with operator_scope():
            self._seed()

    def _seed(self) -> None:
        jarrow, _ = Organization.objects.get_or_create(
            slug="jarrow",
            defaults={"name": "Jarrow Formulas", "status": Organization.Status.ACTIVE},
        )
        fixture, _ = Organization.objects.get_or_create(
            slug="second-client-fixture",
            defaults={
                "name": "Second-client fixture",
                "status": Organization.Status.FIXTURE,
                "is_fixture": True,
            },
        )

        Digest.objects.update_or_create(
            window_start=date(2026, 9, 6),
            window_end=date(2026, 9, 13),
            defaults={"created_at": PREVIOUS_DIGEST_AT},
        )
        Digest.objects.update_or_create(
            window_start=date(2026, 9, 13),
            window_end=date(2026, 9, 20),
            defaults={"created_at": CURRENT_DIGEST_AT},
        )

        for n, (code, title, state, domain, fit, confidence, flags) in enumerate(SIGNALS):
            signal, _ = Signal.objects.update_or_create(
                code=code,
                defaults={
                    "title": title,
                    "state": state,
                    "domain_score": domain,
                    # Spread across the window, all after the previous digest.
                    "first_seen_at": PREVIOUS_DIGEST_AT + timedelta(hours=6 + 8 * n),
                    "awaiting_operator_judgment": "review" in flags,
                    "needs_scientific_routing": "research" in flags,
                },
            )
            self._score(jarrow, signal, fit, confidence)

        for code, fit, confidence in FIXTURE_SCORES:
            self._score(fixture, Signal.objects.get(code=code), fit, confidence)

        sources = {}
        for name, route, status_, last_success in SOURCES:
            sources[name], _ = Source.objects.update_or_create(
                name=name,
                defaults={"route": route, "status": status_, "last_success_at": last_success},
            )

        for name, started, minutes, status_, blocking in RUNS:
            IngestionRun.objects.update_or_create(
                source=sources[name],
                started_at=started,
                defaults={
                    "finished_at": started + timedelta(minutes=minutes),
                    "status": status_,
                    "blocking": blocking,
                },
            )

        self.stdout.write(self.style.SUCCESS("\nSeeded the Triage home.\n"))
        self.stdout.write(
            f"  {len(SIGNALS)} signals, {len(SOURCES)} sources, {len(RUNS)} runs, 2 digests\n"
        )

    def _score(self, org: Organization, signal: Signal, fit: int, confidence: int) -> None:
        ClientSignalScore.objects.update_or_create(
            organization=org,
            signal=signal,
            defaults={"fit_score": fit, "confidence": confidence},
        )
