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
    date_joined = models.DateTimeField(auto_now_add=True)

    # ── Second factor (PRD §7.1) ────────────────────────────────────────────
    #: True once a code from the enrolled authenticator has been verified — not
    #: when a secret is generated. A secret nobody has proved they can read is
    #: not a second factor, it is a lockout waiting to happen.
    mfa_enabled = models.BooleanField(default=False)
    #: Fernet-encrypted. In plaintext, a database dump would let whoever holds
    #: it generate valid codes forever while the account looked normal.
    totp_secret = models.TextField(blank=True)
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    #: The 30-second step of the last accepted code. A TOTP code is valid for
    #: its whole step, so one observed over a shoulder works again until that
    #: step ends; anything at or below this is refused.
    totp_last_counter = models.BigIntegerField(default=0)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    objects = OperatorUserManager()

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email

    @property
    def mfa_ready(self) -> bool:
        """Whether this account can complete a second-factor challenge."""
        return bool(self.mfa_enabled and self.totp_secret)


class RecoveryCode(models.Model):
    """One single-use way back in after a lost authenticator.

    A separate table rather than a JSON list on the user so that consuming one
    is a row update the database can make atomic. Two simultaneous logins using
    the same code would otherwise both read the list, both find it unused, and
    both succeed.

    Stored hashed with the password hasher: a recovery code IS a credential,
    and storing one reversibly would mean the database held a second usable way
    into every operator account.
    """

    #: NOT a ForeignKey, for the reason at the top of this module. Django
    #: deconstructs any FK pointing at whatever `AUTH_USER_MODEL` currently
    #: names into `settings.AUTH_USER_MODEL` — it substitutes the setting even
    #: for a string literal, and even with `Meta.swappable` unset. So this
    #: column would resolve to `operations_operatoruser` in the ops process and
    #: `portal_orguser` in the portal one, over one shared database. That is
    #: the same hazard `AuditEvent` above avoids the same way, and
    #: `tests/test_migrations_are_settings_independent.py` is what caught it.
    #:
    #: The cost is no cascade delete. It is small: codes are only ever looked
    #: up BY operator id, so a row left behind by a deleted account is
    #: unreachable rather than dangerous, and `mfa.reset` clears them anyway.
    operator_id = models.BigIntegerField(db_index=True)
    code_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at",)
        indexes = [models.Index(fields=["operator_id", "used_at"])]

    def __str__(self) -> str:
        return f"recovery code for {self.operator_id} ({'used' if self.used_at else 'unused'})"


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
    #: Six places, not four. A relevance-gate call costs about $0.00195 and the
    #: cheapest ones are an order of magnitude below that — at four places they
    #: round to zero, so the ledger would read empty while real money was being
    #: spent. Since `apps.enrichment.llm.costs` reads this column to enforce the
    #: cap (Arch §10.2), a rounding error here is a cap that never fires.
    estimated_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)

    class Meta:
        ordering = ("-at",)

    def __str__(self) -> str:
        return f"{self.provider} ${self.estimated_usd} at {self.at:%Y-%m-%d %H:%M}"


class BackupRun(models.Model):
    """One backup or restore-test attempt. Arch §11.3, Arch §13.

    Not tenant-scoped: a database dump is infrastructure, not any tenant's
    data. Same shape as `CostEvent` above for that reason.

    This table exists because "did last night's backup run?" has to be
    answerable without SSH. A backup job whose only output is a log line is
    one nobody checks, and the first time anyone looks is after the box is
    gone. The Operations screen reads `latest()` and shows its age.

    Failures are recorded, not just successes — a run that refused because
    `BACKUP_TARGET` is unset must leave a mark, or an unconfigured backup
    looks exactly like a working one.
    """

    class Kind(models.TextChoices):
        BACKUP = "backup", "Nightly backup"
        RESTORE_TEST = "restore_test", "Restore test"

    class Outcome(models.TextChoices):
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped — not configured"

    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    #: The object key in the bucket, so a restore has something to ask for.
    artifact = models.CharField(max_length=300, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    #: Of the dump file. The restore test re-computes it before trusting the
    #: download — a truncated upload restores "successfully" into an empty
    #: database otherwise.
    sha256 = models.CharField(max_length=64, blank=True)
    detail = models.TextField(blank=True)

    class Meta:
        ordering = ("-started_at",)
        indexes = [models.Index(fields=["kind", "-started_at"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.outcome} at {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @classmethod
    def latest(cls, kind: str) -> "BackupRun | None":
        return cls.objects.filter(kind=kind).first()
