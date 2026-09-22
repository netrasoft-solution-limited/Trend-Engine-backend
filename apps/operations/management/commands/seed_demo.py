"""Seed a working development dataset.

Creates the launch tenant, a second-client CI fixture (PRD §2), portal users
including one with TWO memberships so the org switcher can actually be
exercised, and content on both sides of the publication gate — approved but
unpublished, published, and withdrawn.

Idempotent: safe to re-run.

Runs under operator scope, because it writes across tenants. That is an
explicit, greppable opt-out rather than a default — see
`apps.tenancy.context.operator_scope`.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.billing.models import Invoice, Plan, Subscription
from apps.operations.models import OperatorUser
from apps.outputs.models import (
    ExpertReview,
    Output,
    OutputState,
    OutputType,
    OutputVersion,
)
from apps.portal.models import OrgMembership, OrgRole, OrgUser
from apps.publication import services as gate
from apps.tenancy.context import operator_scope
from apps.tenancy.models import Organization

PASSWORD = "portal-demo-2026"


class Command(BaseCommand):
    help = "Seed a development dataset for the portal."

    @transaction.atomic
    def handle(self, *args, **options):
        with operator_scope():
            self._seed()

    def _seed(self) -> None:
        jarrow, _ = Organization.objects.get_or_create(
            slug="jarrow",
            defaults={"name": "Jarrow Formulas", "status": Organization.Status.ACTIVE},
        )
        # PRD §2: the architecture proof. A second tenant with its own content,
        # so isolation tests assert against something rather than nothing.
        fixture, _ = Organization.objects.get_or_create(
            slug="second-client-fixture",
            defaults={
                "name": "Second-client fixture",
                "status": Organization.Status.FIXTURE,
                "is_fixture": True,
            },
        )

        operator, created = OperatorUser.objects.get_or_create(
            email="abubakar@pureplay.example",
            defaults={"name": "Abubakar", "role": OperatorUser.Role.PLATFORM_ADMIN, "is_staff": True},
        )
        if created:
            operator.set_password(PASSWORD)
            operator.save(update_fields=["password"])

        admin = self._user("dana@jarrow.example", "Dana Whitfield")
        viewer = self._user("priya@jarrow.example", "Priya Raman")
        # The multi-org case: one account, two organisations, different roles.
        consultant = self._user("sofia@consultant.example", "Sofia Lindqvist")

        self._member(admin, jarrow, OrgRole.ADMIN)
        self._member(viewer, jarrow, OrgRole.VIEWER)
        self._member(consultant, jarrow, OrgRole.VIEWER)
        self._member(consultant, fixture, OrgRole.ADMIN)

        self._billing(jarrow)
        self._content(jarrow, operator)
        self._fixture_content(fixture, operator)

        self.stdout.write(self.style.SUCCESS("\nSeeded.\n"))
        self.stdout.write(f"  password for every account: {PASSWORD}\n\n")
        self.stdout.write("  dana@jarrow.example        Org Admin  · Jarrow\n")
        self.stdout.write("  priya@jarrow.example       Org Viewer · Jarrow\n")
        self.stdout.write("  sofia@consultant.example   TWO orgs   · Viewer at Jarrow, Admin at the fixture\n")

    # ── helpers ─────────────────────────────────────────────────────────────

    def _user(self, email: str, name: str) -> OrgUser:
        user = OrgUser.objects.filter(email=email).first()
        if user is None:
            user = OrgUser.objects.create_user(email=email, password=PASSWORD, name=name)
        return user

    def _member(self, user: OrgUser, org: Organization, role: str) -> None:
        OrgMembership.objects.update_or_create(
            org_user=user,
            organization=org,
            defaults={
                "role": role,
                "status": OrgMembership.Status.ACTIVE,
                "accepted_at": timezone.now(),
            },
        )

    def _billing(self, org: Organization) -> None:
        plan, _ = Plan.objects.get_or_create(
            code="category-intelligence-single-domain",
            defaults={"name": "Category intelligence — single domain", "amount_monthly": 5000},
        )
        Subscription.objects.get_or_create(
            organization=org,
            defaults={
                "plan": plan,
                "status": Subscription.Status.ACTIVE,
                "current_period_start": date(2026, 9, 1),
                "current_period_end": date(2026, 9, 30),
                "renews_on": date(2026, 10, 1),
                "processor_ref": "tok_live_••••4417",
            },
        )
        for n, (period, status_) in enumerate(
            [("September 2026", Invoice.Status.OPEN), ("August 2026", Invoice.Status.PAID)]
        ):
            Invoice.objects.get_or_create(
                organization=org,
                number=f"INV-2026-{9 - n:02d}",
                defaults={
                    "period_label": period,
                    "amount": 5000,
                    "status": status_,
                    "issued_on": date(2026, 9 - n, 1),
                },
            )

    def _output(self, org, *, type_, title, summary, body, state) -> tuple[Output, OutputVersion]:
        output, _ = Output.objects.get_or_create(
            organization=org,
            title=title,
            defaults={
                "type": type_,
                "state": state,
                "client_profile_version": "jarrow@2026-09-16",
                "domain_pack_version": "supplements@3.4.0",
            },
        )
        version, _ = OutputVersion.objects.get_or_create(
            organization=org,
            output=output,
            number=1,
            defaults={
                "summary": summary,
                "body": body,
                "state": state,
                "created_by_label": "abubakar",
            },
        )
        return output, version

    def _content(self, org: Organization, operator: OperatorUser) -> None:
        actor = operator.email

        # 1. Published — the client can read this.
        output, version = self._output(
            org,
            type_=OutputType.RESEARCH_ALERT,
            title="Creatine + working memory: what the new crossover RCT does and does not show",
            summary=(
                "A small crossover trial reports working-memory improvement under sleep "
                "restriction. Useful context for cognition content — not a general claim."
            ),
            body=[
                {"heading": "In plain language", "text":
                 "A crossover trial gave creatine to sleep-restricted adults and measured working "
                 "memory. Scores improved against placebo within the same participants."},
                {"heading": "Study design", "text":
                 "Randomised crossover, n=24, each participant serving as their own control. "
                 "Crossover designs reduce between-person variation but cannot rule out carry-over."},
                {"heading": "Limitations", "text":
                 "Small sample. Single site. Short duration. One related meta-analysis remains "
                 "inconclusive."},
            ],
            state=OutputState.APPROVED,
        )
        # Research alerts are health/scientific, so the gate refuses without this.
        ExpertReview.objects.get_or_create(
            organization=org,
            output=output,
            reviewer_label="M. Reyes",
            defaults={"signed_off_at": timezone.now(), "note": "Limitations reflect the design."},
        )
        if not output.publications.filter(unpublished_at__isnull=True).exists():
            gate.publish(version=version, organization=org, actor_label=actor)

        # 2. Approved but NOT published — the whole point of the gate. This must
        #    be invisible in the portal.
        self._output(
            org,
            type_=OutputType.PRODUCT_MEMO,
            title="Berberine: portfolio gap and safety exposure",
            summary="Approved for the internal record. Deliberately not published.",
            body=[{"heading": "Internal", "text": "Should never appear in the portal."}],
            state=OutputState.APPROVED,
        )

        # 3. Published then withdrawn — must resolve to nothing, not to an
        #    older version.
        withdrawn_output, withdrawn_version = self._output(
            org,
            type_=OutputType.TREND_BRIEF,
            title="Week of Aug 31 — superseded",
            summary="Withdrawn after a citation error was found in the research section.",
            body=[{"heading": "Withdrawn", "text": "A corrected version was issued."}],
            state=OutputState.APPROVED,
        )
        ExpertReview.objects.get_or_create(
            organization=org,
            output=withdrawn_output,
            reviewer_label="M. Reyes",
            defaults={"signed_off_at": timezone.now()},
        )
        if not withdrawn_output.publications.exists():
            pub = gate.publish(version=withdrawn_version, organization=org, actor_label=actor)
            gate.unpublish(
                publication=pub, actor_label=actor, reason="Citation error in the research section"
            )

        # 4. Still in preparation — appears on the delivery tracker as
        #    "in preparation" and nowhere else.
        self._output(
            org,
            type_=OutputType.CONTENT_BRIEF,
            title="Creatine for cognition — buyer education",
            summary="With the Pure Play team.",
            body=[],
            state=OutputState.DRAFT,
        )

    def _fixture_content(self, org: Organization, operator: OperatorUser) -> None:
        """Content for the second tenant. Its existence is what makes the
        isolation assertions meaningful — a leakage test against an empty
        second tenant proves nothing."""
        output, version = self._output(
            org,
            type_=OutputType.CONTENT_QUEUE,
            title="Fixture tenant content — must never appear under Jarrow",
            summary="Belongs to the second-client CI fixture.",
            body=[{"heading": "Fixture", "text": "PRD §2 architecture proof. Not a live client."}],
            state=OutputState.APPROVED,
        )
        if not output.publications.filter(unpublished_at__isnull=True).exists():
            gate.publish(version=version, organization=org, actor_label=operator.email)
