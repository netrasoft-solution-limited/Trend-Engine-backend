"""Define, inspect and activate a client profile from a JSON file.

There is no operator screen for this yet, and a client profile is the one thing
the system cannot infer — it is commercial knowledge that lives with the
operator. A file is a better interim than a script: it is reviewable, it can be
sent to the client to correct, it diffs, and it is the same shape the Client
Profile screen will post when it exists.

    manage.py client_profile show    jarrow
    manage.py client_profile load    jarrow profiles/jarrow.json --activate
    manage.py client_profile score   jarrow

The file:

    {
      "label": "Q4 2026 — added the sleep range",
      "assets": [
        {"name": "MagMind", "kind": "product", "weight": 90,
         "terms": ["magnesium l-threonate", "threonate", "magmind"]}
      ],
      "audiences":   [{"name": "Adults over 50", "weight": 60, "terms": ["older adults"]}],
      "priorities":  [{"name": "Sleep quality",  "weight": 70, "terms": ["sleep", "insomnia"]}],
      "competitors": [{"name": "A rival brand",  "weight": 50, "terms": ["rivalbrand"]}]
    }

Loading always creates a NEW version rather than editing the live one, because
published briefs cite the version they were built from (PRD §8) and a profile
that changes underneath them makes those briefs unexplainable.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.clients import services
from apps.clients.models import ClientAsset, ClientProfileVersion, ClientTerm
from apps.tenancy.context import operator_scope, scoped
from apps.tenancy.models import Organization

FACETS = {
    "audiences": ClientTerm.Facet.AUDIENCE,
    "priorities": ClientTerm.Facet.PRIORITY,
    "competitors": ClientTerm.Facet.COMPETITOR,
}


class Command(BaseCommand):
    help = "Define, inspect, activate or apply a client profile."

    def add_arguments(self, parser) -> None:
        parser.add_argument("action", choices=["show", "load", "score", "discover"])
        parser.add_argument("slug", help="The organisation's slug.")
        parser.add_argument("path", nargs="?", help="The JSON file, for `load`.")
        parser.add_argument(
            # Not `--version`: BaseCommand already defines that, and argparse
            # raises at parser construction rather than at call time, so the
            # whole command becomes unrunnable.
            "--profile-version",
            type=int,
            help="For `show`: a specific version instead of the live one.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="For `show`: emit the profile in `load` format, so a discovered "
                 "draft can be exported, edited and activated.",
        )
        parser.add_argument(
            "--url",
            help="The client's storefront, for `discover` — e.g. https://jarrow.com",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Make the loaded version live. Without it the version is drafted "
                 "and left for review — nothing cites it until it is activated. "
                 "`discover` ignores this: a derived profile is always reviewed.",
        )

    def handle(self, *args, **options) -> None:
        with operator_scope():
            organization = Organization.objects.filter(slug=options["slug"]).first()
        if organization is None:
            raise CommandError(f"No organisation with slug {options['slug']!r}.")

        with scoped(organization):
            getattr(self, f"_{options['action']}")(organization, options)

    # ── show ────────────────────────────────────────────────────────────────

    def _show(self, organization, options) -> None:
        if options.get("profile_version"):
            version = ClientProfileVersion.objects.filter(number=options["profile_version"]).first()
            if version is None:
                raise CommandError(f"No profile v{options['version']} for {organization.name}.")
        else:
            version = services.current_for(organization)

        # The export half of the round trip: `discover` drafts a profile, this
        # emits it in the same shape `load` accepts, and the operator edits the
        # weights in between. Without it, a discovered draft is a dead end.
        if options["json"] and version is not None:
            self.stdout.write(json.dumps(services.to_spec(version), indent=2))
            return

        if version is None:
            self.stdout.write(
                self.style.WARNING(f"{organization.name} has no active profile.")
            )
            return

        self.stdout.write(
            self.style.SUCCESS(f"{organization.name} — profile v{version.number}")
        )
        if version.label:
            self.stdout.write(f"  {version.label}")

        for asset in version.assets.all():
            self.stdout.write(
                f"  asset      {asset.weight:>3}  {asset.name} "
                f"[{asset.get_kind_display()}] — {', '.join(asset.terms)}"
            )
        for term in version.terms.all():
            self.stdout.write(
                f"  {term.facet:<10} {term.weight:>3}  {term.name} — {', '.join(term.terms)}"
            )

    # ── load ────────────────────────────────────────────────────────────────

    def _load(self, organization, options) -> None:
        if not options["path"]:
            raise CommandError("`load` needs a path to a JSON file.")
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        try:
            spec = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

        # Copying is off: a file is the whole profile. Merging it onto the
        # previous version would make "what is in this profile?" a question
        # about history rather than about the file in front of you.
        version = services.draft(
            organization,
            label=spec.get("label", path.name),
            actor_label="client_profile command",
            copy_current=False,
        )

        for row in spec.get("assets", []):
            ClientAsset.objects.create(
                organization=organization,
                profile=version,
                name=row["name"],
                kind=row.get("kind", ClientAsset.Kind.PRODUCT),
                terms=row.get("terms", []),
                weight=row.get("weight", 50),
                notes=row.get("notes", ""),
            )
        for key, facet in FACETS.items():
            for row in spec.get(key, []):
                ClientTerm.objects.create(
                    organization=organization,
                    profile=version,
                    facet=facet,
                    name=row["name"],
                    terms=row.get("terms", []),
                    weight=row.get("weight", 50),
                    notes=row.get("notes", ""),
                )

        self.stdout.write(
            f"Drafted v{version.number}: {version.assets.count()} asset(s), "
            f"{version.terms.count()} term(s)."
        )

        if not options["activate"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Not activated. Nothing cites it yet — re-run with --activate "
                    f"to make it live."
                )
            )
            return

        try:
            services.activate(version, actor_label="client_profile command")
        except services.ProfileError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"v{version.number} is now live."))

    # ── score ───────────────────────────────────────────────────────────────

    def _score(self, organization, options) -> None:
        from apps.scoring import relevance

        try:
            summary = relevance.score_for(organization)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"{summary['organization']} v{summary['profile_version']}: "
                f"{summary['matched']} of {summary['claims']} claims matched."
            )
        )
        if summary["matched"] == 0:
            self.stdout.write(
                self.style.WARNING(
                    "Nothing matched. The profile's terms are probably narrower than "
                    "the words people actually say — check a claim you expected to "
                    "match and add the term it uses."
                )
            )

    # ── discover ────────────────────────────────────────────────────────────

    def _discover(self, organization, options) -> None:
        from apps.clients import catalogue, discovery

        if not options["url"]:
            raise CommandError("`discover` needs --url, the client's storefront.")

        self.stdout.write(f"Reading {options['url']} …")
        try:
            products = catalogue.read(options["url"])
        except catalogue.CatalogueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"  {len(products)} product(s) found. Interpreting …")
        version, discovered = discovery.draft_from_catalogue(
            organization, products=products, actor_label="discover command"
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDrafted v{version.number} for {organization.name}"
            )
        )
        self.stdout.write(f"  category   {discovered.category}")
        if discovered.adjacent_categories:
            self.stdout.write(f"  adjacent   {', '.join(discovered.adjacent_categories)}")
        for asset in version.assets.all():
            self.stdout.write(f"  asset      {asset.name} — {', '.join(asset.terms[:6])}")
        for term in version.terms.all():
            self.stdout.write(f"  {term.facet:<10} {term.name} — {', '.join(term.terms[:6])}")

        self.stdout.write(
            self.style.WARNING(
                "\nNot activated, on purpose. Every weight is a flat 50 and no "
                "competitors were derived — a storefront says what a client sells, "
                "not what they are pushing or who they are trying to reach.\n"
                "Review it, then activate by exporting to JSON, editing, and "
                "running `client_profile load` with --activate."
            )
        )
