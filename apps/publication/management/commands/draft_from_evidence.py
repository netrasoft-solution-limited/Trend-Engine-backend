"""Build a brief from stored evidence and put it through the real gates.

LIVES IN `publication`, NOT `outputs`. It spans both — drafting and then
publishing — and `apps.publication` sits ABOVE `apps.outputs` in the layer
stack, so only this side may import both. Put the other way round it reads
just as naturally and `lint-imports` refuses it, which is how this landed here.

The Output Builder screen is the normal path now; this remains for scripting.
It calls the same three services the screen does — `draft`, `approve`,
`record_expert_signoff` — and then `publication.publish()`, so nothing here is
a shortcut around the gates:

  · an unapproved version is refused by `NotApproved`
  · a health or scientific type without a recorded sign-off is refused by
    `ExpertReviewMissing`

Both refusals are worth seeing, which is why `--skip-signoff` exists.

CHANGED 20 OCT. This command produced the 1 October brief: from the whole
corpus rather than a client's profile, under a fixed profile label that matched
nothing stored, with a sign-off whose reviewer was whatever `--reviewer` said
and whose note was written by this file. It now refuses without a live client
profile, drafts from that profile, cites its stored version, and requires a
registered reviewer who is not the actor. It writes no note on a reviewer's
behalf.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.outputs import drafting, services
from apps.outputs.models import OutputType
from apps.publication import services as gate
from apps.tenancy.context import operator_scope
from apps.tenancy.models import Organization


class Command(BaseCommand):
    help = "Draft a brief from stored claims, approve it, and publish it to one organisation."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--org", required=True, help="Organisation slug, e.g. jarrow.")
        parser.add_argument("--title", default="", help="Defaults to a dated trend brief.")
        parser.add_argument(
            "--actor", default="abubakar@netrasoft", help="Recorded in the audit log."
        )
        parser.add_argument(
            "--reviewer", default="", help="A registered expert reviewer (expert_reviewer list)."
        )
        parser.add_argument("--note", default="", help="The reviewer's own note, if any.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Print the sections and write nothing."
        )
        parser.add_argument(
            "--skip-signoff",
            action="store_true",
            help="Approve without an expert review, to demonstrate the gate refusing.",
        )
        parser.add_argument(
            "--no-publish",
            action="store_true",
            help="Draft and approve only — approval is not publication (PRD §6.9).",
        )

    def handle(self, *args, **options) -> None:
        with operator_scope():
            self._run(options)

    def _run(self, options) -> None:
        from django.utils import timezone

        organization = Organization.objects.filter(slug=options["org"]).first()
        if organization is None:
            raise CommandError(f"No organisation with slug {options['org']!r}.")

        from apps.clients import services as client_services
        from apps.enrichment.domain import DOMAIN_PACK_VERSION
        from apps.tenancy.context import scoped

        with scoped(organization):
            profile = client_services.current_for(organization)
            if profile is None:
                raise CommandError(
                    f"{organization} has no live client profile. A brief is drafted for a "
                    f"client, against the profile version it cites."
                )
            try:
                sections = drafting.sections_for(organization)
            except services.DraftingError as exc:
                raise CommandError(str(exc)) from exc

        if options["dry_run"]:
            for section in sections:
                self.stdout.write(self.style.SUCCESS(f"\n## {section['heading']}\n"))
                self.stdout.write(section["text"][:900])
            self.stdout.write(f"\n{len(sections)} sections. Nothing written.")
            return

        if not options["skip_signoff"] and not options["reviewer"]:
            raise CommandError("Name the registered expert reviewer with --reviewer.")

        title = options["title"] or f"Category brief — {timezone.now():%B %Y}"
        actor = options["actor"]

        version = services.draft(
            organization,
            type=OutputType.TREND_BRIEF,
            title=title,
            sections=sections,
            summary=(
                "What the category's most-followed voices said this period, quoted verbatim "
                "and timestamped so every line can be checked against its source."
            ),
            actor_label=actor,
            client_profile_version=profile.citation,
            domain_pack_version=DOMAIN_PACK_VERSION,
        )
        self.stdout.write(f"  drafted  v{version.number} — {len(sections)} sections")

        if not options["skip_signoff"]:
            try:
                services.record_expert_signoff(
                    version.output,
                    reviewer_label=options["reviewer"],
                    actor_label=actor,
                    discipline="Scientific / legal claims",
                    note=options["note"],
                )
            except services.DraftingError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(f"  signed   expert review recorded by {options['reviewer']}")

        services.approve(version, actor_label=actor)
        self.stdout.write("  approved internal sign-off — NOT client-visible yet")

        if options["no_publish"]:
            self.stdout.write(
                self.style.WARNING(
                    "  stopped  approved but unpublished, which is the point of PRD §6.9"
                )
            )
            return

        try:
            publication = gate.publish(
                version=version, organization=organization, actor_label=actor
            )
        except gate.ExpertReviewMissing as exc:
            raise CommandError(f"The gate refused it, correctly: {exc}") from exc
        except gate.NotApproved as exc:
            raise CommandError(f"The gate refused it, correctly: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"  published #{publication.pk} — now visible to {organization.name}"
            )
        )
