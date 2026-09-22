"""billing — L7, TENANT-SCOPED.

PRD §8 models: Subscription, Plan, Invoice, PaymentRecord.

`Organization` is NOT here. Arch §4 lists it under this app, but every
tenant-scoped model from L5 up carries a foreign key to the tenant root, so
keeping that root at L7 would invert the dependency rule — `clients` (L5) would
have to import from `billing` (L7). It lives in `apps.tenancy` (L0) instead,
and this app points down at it. Recorded in backend/README.md.

PRD §7.5 is a hard constraint, not a preference: no payment card or bank
account data is ever stored in this database. Processor tokens only.
"""
from __future__ import annotations

from django.db import models

from apps.tenancy.managers import TenantScopedModel


class Plan(models.Model):
    """Global, not tenant-scoped — a plan is a product, not a customer.

    PRD §12 leaves the commercial model undecided (subscription vs service
    engagement, flat vs tiered). This shape supports flat-per-client, which is
    what §11 assumes, without foreclosing tiers.
    """

    code = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    amount_monthly = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class Subscription(TenantScopedModel):
    """Status tracking only at launch.

    PRD §6.8: "Automated collection and invoicing are out of scope at launch —
    with a single client, manual invoicing is cheaper than building automation.
    The data model must support automation later without migration."
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAST_DUE = "past due", "Past due"
        CANCELLED = "cancelled", "Cancelled"

    plan = models.ForeignKey("billing.Plan", on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    current_period_start = models.DateField()
    current_period_end = models.DateField()
    renews_on = models.DateField(null=True, blank=True)

    #: PRD §7.5 / Arch §12. A reference issued by a PCI-compliant processor.
    #: Never a PAN, never a bank account, never anything that could be one.
    processor_ref = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ("-current_period_start",)


class Invoice(TenantScopedModel):
    class Status(models.TextChoices):
        PAID = "paid", "Paid"
        OPEN = "open", "Open"
        VOID = "void", "Void"

    number = models.CharField(max_length=64)
    period_label = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    issued_on = models.DateField()
    method = models.CharField(max_length=64, default="Manual — bank transfer")

    class Meta:
        ordering = ("-issued_on",)
        constraints = [
            models.UniqueConstraint(fields=["organization", "number"], name="uniq_invoice_number")
        ]

    def __str__(self) -> str:
        return self.number
