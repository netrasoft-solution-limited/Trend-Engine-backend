"""operations — cross-cutting, GLOBAL.

PRD §8 models: AuditEvent, CostEvent, DeadLetterItem, plus the operator realm's
user model.

Tenant-agnostic by design. Nothing in this app may import from clients,
scoring, outputs, publication, portal or billing (Arch §4).

NOTE ON USER FOREIGN KEYS
-------------------------
No model here may use `settings.AUTH_USER_MODEL`. Django records that as a
`SettingsReference` resolved at import time, so the same column would point at
`operations_operatoruser` in the ops process and `portal_orguser` in the portal
process — two identity tables joined on the same integer key, silently. Every
user FK names its concrete target as a string literal, and CI proves the
migration set is settings-independent by running `makemigrations --check` under
both settings modules.
"""
from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class OperatorUserManager(BaseUserManager):
    """Email-keyed. There is no open registration anywhere (PRD §7.1)."""

    use_in_migrations = True

    def _create(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("Operator accounts require an email address")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.password = make_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create(email, password, **extra)


class OperatorUser(AbstractBaseUser, PermissionsMixin):
    """Pure Play staff. The operator realm's user — `/ops/*` only.

    Carries `PermissionsMixin` because operators plausibly want granular
    permissions and a future ops-only admin needs them. `portal.OrgUser`
    deliberately does not: see the note on that model.

    PRD §3.2 keeps Operator and Platform Admin as distinct roles even where one
    person holds both, so that separating them later needs no schema change.
    """

    class Role(models.TextChoices):
        OPERATOR = "operator", "Operator"
        PLATFORM_ADMIN = "platform_admin", "Platform Admin"

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=32, choices=Role.choices, default=Role.OPERATOR)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    #: PRD §7.1 requires MFA where supported; `ops.py` sets OPERATOR_REQUIRE_MFA.
    mfa_enabled = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    objects = OperatorUserManager()

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email


class AuditEvent(models.Model):
    """Immutable record of config, review, approval, publication and export
    events (PRD §7.1).

    The actor is DENORMALISED on purpose — no foreign key:

      · An audit row must outlive the account it names. A user deletion, which
        PRD §7.6 requires be possible on request, must not cascade away the
        history of what they did.
      · The actor may be an operator, an org user, or the system. Those live in
        two different identity tables, and a nullable FK to each would invite
        exactly the `settings.AUTH_USER_MODEL` reference this app forbids.
      · `actor_label` snapshots the email at event time, so the record still
        reads correctly after a rename or an erasure.
    """

    class Kind(models.TextChoices):
        CONFIG = "config", "Config"
        REVIEW = "review", "Review"
        APPROVAL = "approval", "Approval"
        PUBLICATION = "publication", "Publication"
        EXPORT = "export", "Export"
        SYSTEM = "system", "System"

    class Realm(models.TextChoices):
        OPERATOR = "operator", "Operator"
        ORG = "org", "Org user"
        SYSTEM = "system", "System"

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)

    actor_realm = models.CharField(max_length=16, choices=Realm.choices)
    actor_id = models.BigIntegerField(null=True, blank=True)
    actor_label = models.CharField(max_length=254)

    #: Which tenant the event concerned, where it concerned one. Not a tenant
    #: scope — the operator plane reads across all of them.
    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )

    message = models.TextField()
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-at",)
        indexes = [models.Index(fields=["organization", "-at"])]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.kind} {self.actor_label}"


class CostEvent(models.Model):
    """One external call. Arch §10.1: the cost ledger.

    Written per request to a vendor so that caps can be enforced BEFORE a run
    starts rather than reconciled after (Arch §10.2).
    """

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    provider = models.CharField(max_length=64, db_index=True)
    source_id = models.CharField(max_length=64, blank=True)
    run_id = models.CharField(max_length=64, blank=True)
    units = models.JSONField(default=dict)
    estimated_usd = models.DecimalField(max_digits=10, decimal_places=4, default=0)

    class Meta:
        ordering = ("-at",)
