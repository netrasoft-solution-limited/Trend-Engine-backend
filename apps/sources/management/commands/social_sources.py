"""Standing TikTok, Instagram and Reddit searches, one per topic stream.

    manage.py social_sources topics.json [--platforms tiktok,instagram,reddit]

`topics.json` is `[{"topic": "Bone health", "query": "vitamin k2 d3"}, ...]`:
Pure Play's budget model is 14 product families plus the new-product
candidates, one search per topic and platform, run weekly (Mark, 31 Oct).

Safe to run again: sources are matched by name and updated. A platform whose
provider has no key yet is created paused (no schedule), so it neither runs nor
fails; run this again once the key is entered to put it on the weekly schedule.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

WEEKLY = 60 * 24 * 7
LABELS = {"tiktok": "TikTok", "instagram": "Instagram", "reddit": "Reddit"}
#: Reddit is charged per post kept; 20 a topic a week is about $2 a month.
REDDIT_LIMIT = 20

POLICIES = {
    "virlo": dict(
        kind=AcquisitionProvider.Kind.VIRLO,
        name="Virlo",
        version="2026-11",
        access_basis=(
            "Virlo raw-data API (dev.virlo.ai) on Pure Play's prepaid account: public "
            "TikTok and Instagram Reels search results, TikTok transcripts and public "
            "comments. Chosen and funded by Pure Play (Mark Costigliola, email of 31 Oct "
            "2026). Virlo's own AI reports are not used."
        ),
        approved_by="Mark Costigliola (Pure Play), email of 31 Oct 2026",
        cap="10.00",
    ),
    "reddit": dict(
        kind=AcquisitionProvider.Kind.APIFY,
        name="Apify",
        version="2026-11-reddit",
        access_basis=(
            "Apify Actor shahab_adl/reddit-scraper-no-login on our existing Apify Starter "
            "account: public Reddit posts, collected without logging in. Named by Pure "
            "Play (Mark Costigliola, email of 31 Oct 2026)."
        ),
        approved_by="Mark Costigliola (Pure Play), email of 31 Oct 2026",
        cap="",
    ),
}


class Command(BaseCommand):
    help = "Create or update weekly TikTok, Instagram and Reddit searches per topic."

    def add_arguments(self, parser):
        parser.add_argument("topics", help="JSON list of {topic, query}")
        parser.add_argument("--platforms", default="tiktok,instagram,reddit")

    @transaction.atomic
    def handle(self, *args, topics, platforms, **options):
        try:
            streams = json.loads(Path(topics).read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot read {topics}: {exc}") from exc
        chosen = [p.strip() for p in platforms.split(",") if p.strip()]
        unknown = set(chosen) - set(LABELS)
        if unknown:
            raise CommandError(f"Unknown platform(s): {', '.join(sorted(unknown))}")

        for platform in chosen:
            policy = self._policy("reddit" if platform == "reddit" else "virlo")
            ready = policy.provider.has_credential or bool(policy.provider.credential())
            for stream in streams:
                name = f"{LABELS[platform]} — {stream['topic']}"
                config = {"platform": platform, "queries": [stream["query"]]}
                if platform == "reddit":
                    config["limit"] = REDDIT_LIMIT
                source, created = Source.objects.update_or_create(
                    name=name,
                    defaults={"route": Source.Route.SOCIAL, "config": config, "policy": policy,
                              "poll_interval_minutes": WEEKLY if ready else 0},
                )
                self.stdout.write(f"{'created' if created else 'updated'} {source.pk} {name}"
                                  + ("" if ready else " (paused: no key yet)"))
            if not ready:
                self.stdout.write(self.style.WARNING(
                    f"{policy.provider.name} has no key. Enter it under Vendor keys, then run "
                    f"this again to schedule the {LABELS[platform]} searches."
                ))

    def _policy(self, which: str) -> ProviderPolicyVersion:
        spec = POLICIES[which]
        provider, _ = AcquisitionProvider.objects.get_or_create(
            kind=spec["kind"], defaults={"name": spec["name"]}
        )
        if spec["cap"] and not provider.monthly_cap_usd:
            provider.monthly_cap_usd = spec["cap"]
            provider.save(update_fields=["monthly_cap_usd"])
        policy, _ = ProviderPolicyVersion.objects.get_or_create(
            provider=provider, version=spec["version"],
            defaults={"access_basis": spec["access_basis"], "approved_at": timezone.now(),
                      "approved_by_label": spec["approved_by"]},
        )
        return policy
