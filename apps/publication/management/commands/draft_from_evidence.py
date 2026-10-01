"""Build a brief from stored evidence and put it through the real gates.

LIVES IN `publication`, NOT `outputs`. It spans both — drafting and then
publishing — and `apps.publication` sits ABOVE `apps.outputs` in the layer
stack, so only this side may import both. Put the other way round it reads
just as naturally and `lint-imports` refuses it, which is how this landed here.

The Output Builder screen (PRD §6.5) does not exist, and this is the seam that
lets an output exist before it does. It calls the same three services a screen
would — `draft`, `approve`, `record_expert_signoff` — and then
`publication.publish()`, so nothing here is a shortcut around the gates:

  · an unapproved version is refused by `NotApproved`
  · a health or scientific type without a recorded sign-off is refused by
    `ExpertReviewMissing`

Both refusals are worth seeing, which is why `--skip-signoff` exists.
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
        parser.add_argument("--actor", default="abubakar@netrasoft", help="Recorded in the audit log.")
        parser.add_argument("--reviewer", default="", help="Named expert reviewer.")
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

        sections = drafting.sections_from_claims()
        if not sections:
            raise CommandError(
                "No claims to draft from. Run the pipeline first — nothing is invented here."
            )

        if options["dry_run"]:
            for section in sections:
                self.stdout.write(self.style.SUCCESS(f"\n## {section['heading']}\n"))
                self.stdout.write(section["text"][:900])
            self.stdout.write(f"\n{len(sections)} sections. Nothing written.")
            return

        organization = Organization.objects.filter(slug=options["org"]).first()
        if organization is None:
            raise CommandError(f"No organisation with slug {options['org']!r}.")

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
            client_profile_version="jarrow@2026-10-01",
            domain_pack_version="supplements@0.1.0",
        )
        self.stdout.write(f"  drafted  v{version.number} — {len(sections)} sections")

        if not options["skip_signoff"]:
            reviewer = options["reviewer"] or actor
            services.record_expert_signoff(
                version.output,
                reviewer_label=reviewer,
                discipline="Scientific / legal claims",
                note="Quotes verified verbatim against stored transcripts.",
            )
            self.stdout.write(f"  signed   expert review recorded by {reviewer}")

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
