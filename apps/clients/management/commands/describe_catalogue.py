"""Add each listing's manufacturer description to a new profile version.

    manage.py describe_catalogue jarrow-formulas [--activate]

Found on 30 Oct: the profile named Gut & Protein Optimizer but never said what
it is, so a recommendation asked Jarrow to "confirm whether it contains any
digestive enzyme" while the same brief quoted the page calling it a "probiotic
and digestive enzyme blend". Each listing whose notes link a Shopify product
page gets the page's own description, its opening sentences, labelled as the
manufacturer's words and dated. Drafts a new version (the live one is never
edited in place); `--activate` makes it live.
"""
from __future__ import annotations

import html
import re
import time

import httpx
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.clients import services
from apps.tenancy.context import scoped
from apps.tenancy.models import Organization

PRODUCT_URL = re.compile(r"https://[^\s·]+/products/[A-Za-z0-9\-_]+")
MARKER = "Manufacturer description"
#: Enough of the copy to say what the product is; product pages open with a
#: pitch before they get there.
MAX_CHARS = 500


#: Seconds between requests. The store answers 429 to anything quicker, and a
#: catalogue of a hundred listings is a one-off job, not a race.
PAUSE = 2.0


def describe(url: str, client: httpx.Client) -> str:
    for attempt in range(4):
        response = client.get(f"{url}.json")
        if response.status_code != 429:
            break
        time.sleep(PAUSE * 5 * (attempt + 1))
    response.raise_for_status()
    body = response.json()["product"].get("body_html") or ""
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))).strip()
    # Shopify copy opens with an all-capitals headline; it is not a description.
    text = re.sub(r"^[A-Z0-9 ,'&\-!]{12,}?(?=[A-Z][a-z])", "", text).strip()
    text = text.replace("*", "").replace("^", "")
    kept: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if kept and len(" ".join(kept + [sentence])) > MAX_CHARS:
            break
        kept.append(sentence)
    return " ".join(kept).strip()


class Command(BaseCommand):
    help = "Draft a profile version whose listings carry their manufacturer descriptions."

    def add_arguments(self, parser) -> None:
        parser.add_argument("organization", help="The organisation's slug.")
        parser.add_argument("--activate", action="store_true")

    def handle(self, *args, **options) -> None:
        organization = Organization.objects.filter(slug=options["organization"]).first()
        if organization is None:
            raise CommandError(f"No organisation {options['organization']!r}.")
        today = timezone.localdate()
        with scoped(organization):
            version = services.draft(
                organization,
                label=f"manufacturer descriptions {today:%-d %b %Y}",
                actor_label="describe_catalogue",
            )
            done = skipped = 0
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                for asset in version.assets.all():
                    match = PRODUCT_URL.search(asset.notes or "")
                    if not match or MARKER in asset.notes:
                        skipped += 1
                        continue
                    time.sleep(PAUSE)
                    try:
                        words = describe(match.group(0), client)
                    except (httpx.HTTPError, KeyError, ValueError) as exc:
                        self.stderr.write(f"{asset.name}: {exc}")
                        skipped += 1
                        continue
                    if not words:
                        skipped += 1
                        continue
                    asset.notes = (
                        f"{asset.notes.rstrip()} {MARKER} ({match.group(0)}, checked "
                        f"{today:%-d %b %Y}; the manufacturer's words, not approved claims): "
                        f"{words}"
                    )
                    asset.save(update_fields=["notes"])
                    done += 1
            if options["activate"]:
                services.activate(version, actor_label="describe_catalogue")
        self.stdout.write(self.style.SUCCESS(
            f"Profile v{version.number}: {done} listing(s) described, {skipped} left as they were"
            + (" — now live." if options["activate"] else " — a draft; review and make it live.")
        ))
