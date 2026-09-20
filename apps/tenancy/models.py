"""The tenant root.

DEVIATION FROM Arch §4, flagged deliberately.

The architecture lists `Organization` under the `billing` app (L7). It cannot
live there. Every tenant-scoped model from L5 upward carries a foreign key to
the tenant root, so placing that root at L7 would invert the dependency rule —
`clients` (L5) would have to import from `billing` (L7).

So `Organization` lives here, at L0, below everything. `billing` keeps what the
architecture actually gives it as responsibilities: Subscription, Plan, Invoice
and PaymentRecord, all pointing down at this model.

PRD §6.8 is unaffected: Organization still maps 1:1 to Client, additively.
"""
from __future__ import annotations

from django.db import models


class Organization(models.Model):
    """One tenant. The boundary every scoped row carries.

    PRD §5 principle 9: "Tenancy is structural, not conditional." This model
    lands in the first migration whether or not a second client ever signs —
    retrofitting it later is the expensive scenario the architecture exists to
    avoid (Arch §16).
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ONBOARDING = "onboarding", "Onboarding"
        SUSPENDED = "suspended", "Suspended"
        FIXTURE = "fixture", "Test fixture"

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ONBOARDING)

    #: PRD §2: the second-client and non-supplement fixtures are CI proofs.
    #: Flagged so they can never be mistaken for, or billed as, live clients.
    is_fixture = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name
