"""portal — L7, TENANT-SCOPED.

PRD §8 models: OrgUser, OrgMembership, OrgInvite, PortalNotification,
PortalLoginEvent.

TWO EXCEPTIONS TO THE TENANT-SCOPING RULE, BOTH DELIBERATE
----------------------------------------------------------
Every tenant-scoped model uses `TenantScopedManager` as its default manager.
`OrgUser`, `OrgMembership` and `OrgInvite` do NOT, and cannot:

    AuthenticationMiddleware sets request.user lazily. Resolving it calls
    OrgUser._default_manager.get(pk=...). That happens BEFORE any tenant is
    bound — binding needs the user, and the user needs the query. A scoped
    default manager here deadlocks every authenticated request, and
    PortalBackend.authenticate() has the same problem: it must find a user by
    email before any organisation is known.

So these three carry plain managers. Their access control comes from elsewhere:
the middleware binds only an organisation the user holds an active membership
in, and every *other* portal model is scoped normally. `OrgMembership` also
exposes `scoped` for the team-management screen, which must only ever show the
active organisation's members.

Do not "fix" this by giving them TenantScopedManager. It will take the portal
down on the first login.

NOTE ON USER FOREIGN KEYS
-------------------------
No model here may use `settings.AUTH_USER_MODEL` — see the equivalent note in
apps/operations/models.py. Concrete string targets only.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.db import models
from django.utils import timezone

from apps.tenancy.managers import TenantScopedManager, TenantScopedModel


class OrgRole(models.TextChoices):
    """PRD §3.2. Two roles, and they live on the MEMBERSHIP, not the user —
    the same person can be an Org Admin at one client and an Org Viewer at
    another."""

    ADMIN = "org_admin", "Org Admin"
    VIEWER = "org_viewer", "Org Viewer"


class OrgUserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email: str, password: str | None = None, **extra):
        if not email:
            raise ValueError("Portal accounts require an email address")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        # An invited user has no password until they accept; `set_unusable_password`
        # is what makes the account unloggable in the meantime.
        user.password = make_password(password)
        user.save(using=self._db)
        return user


class OrgUser(AbstractBaseUser):
    """A client-side person. The tenant realm's user — `/portal/*` only.

    Deliberately NOT a PermissionsMixin subclass:

      · Two PermissionsMixin subclasses both declare `Group.user_set`, which is
        a `fields.E304` system-check failure that blocks startup.
      · Without the mixin there is no join table between an OrgUser and an
        operator `Permission`. An org user is *structurally* incapable of
        holding operator permissions, rather than merely not being granted any.

    Email is globally unique. Roles are per-membership, so a consultant at two
    client organisations has one account and two memberships.
    """

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(auto_now_add=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    #: Plain manager — see the module docstring. Scoping happens per membership.
    objects = OrgUserManager()

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email

    def active_memberships(self):
        return self.memberships.filter(
            status=OrgMembership.Status.ACTIVE,
            organization__status__in=("active", "onboarding", "fixture"),
        ).select_related("organization")

    def membership_for(self, organization_id):
        """The active membership for one organisation, or None.

        This is the ONLY sanctioned way to answer "may this user act in this
        organisation". The middleware calls it on every request, against the
        organisation id carried in the session — which is attacker-controllable
        and is never trusted on its own.
        """
        return self.active_memberships().filter(organization_id=organization_id).first()


class OrgMembership(models.Model):
    """The join that makes multi-org possible, and the thing that must be
    re-verified on every request.

    Plain default manager (see the module docstring) because the middleware
    queries it *in order to* establish the tenant scope. `scoped` is provided
    for views that list the current organisation's members.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INVITED = "invited", "Invited"
        SUSPENDED = "suspended", "Suspended"

    org_user = models.ForeignKey(
        "portal.OrgUser", on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        "tenancy.Organization", on_delete=models.PROTECT, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.VIEWER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INVITED)

    #: PRD §6.8: invite-based provisioning only. Who invited whom is auditable.
    invited_by = models.ForeignKey(
        "portal.OrgUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invitations_sent",
    )
    invited_by_operator = models.BigIntegerField(
        null=True, blank=True, help_text="operations.OperatorUser id, denormalised — no cross-realm FK"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()
    scoped = TenantScopedManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["org_user", "organization"], name="uniq_membership_per_org"
            )
        ]
        ordering = ("organization", "org_user")

    def __str__(self) -> str:
        return f"{self.org_user_id}@{self.organization_id} ({self.role})"

    @property
    def is_admin(self) -> bool:
        return self.role == OrgRole.ADMIN


def _invite_expiry():
    return timezone.now() + timedelta(days=7)


class OrgInvite(models.Model):
    """A single-use invitation. PRD §6.8: there is no self-serve signup path.

    The raw token is shown once, in the email, and never stored — only its
    hash. A leaked database therefore does not yield usable invitations.
    """

    organization = models.ForeignKey(
        "tenancy.Organization", on_delete=models.CASCADE, related_name="invites"
    )
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.VIEWER)

    token_hash = models.CharField(max_length=128, unique=True)
    expires_at = models.DateTimeField(default=_invite_expiry)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    invited_by = models.ForeignKey(
        "portal.OrgUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()

    class Meta:
        ordering = ("-created_at",)

    @staticmethod
    def new_token() -> str:
        return secrets.token_urlsafe(32)

    @property
    def is_usable(self) -> bool:
        return (
            self.accepted_at is None
            and self.revoked_at is None
            and self.expires_at > timezone.now()
        )


class PortalLoginEvent(models.Model):
    """PRD §6.8 requires an audit trail of portal logins; §7.6 sets retention
    at 730 days.

    Failures are recorded as well as successes — a login audit that only shows
    successes cannot show you an attack.

    `org_user` is nullable because a failed attempt may name an address that
    does not exist, and we still want the attempt. `email_attempted` is stored
    verbatim for that case.
    """

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        BAD_CREDENTIALS = "bad_credentials", "Bad credentials"
        INACTIVE = "inactive", "Account inactive"
        NO_MEMBERSHIP = "no_membership", "No active membership"
        RATE_LIMITED = "rate_limited", "Rate limited"

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    org_user = models.ForeignKey(
        "portal.OrgUser", on_delete=models.SET_NULL, null=True, blank=True, related_name="login_events"
    )
    email_attempted = models.EmailField()
    outcome = models.CharField(max_length=32, choices=Outcome.choices)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)

    objects = models.Manager()

    class Meta:
        ordering = ("-at",)
        indexes = [models.Index(fields=["email_attempted", "-at"])]


class PortalNotification(TenantScopedModel):
    """Scoped normally — it is read by portal views after the tenant is bound."""

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        IN_APP = "in_app", "In-app"

    publication = models.ForeignKey(
        "publication.Publication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    subject = models.CharField(max_length=300)
    body = models.TextField(blank=True)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.EMAIL)
    sent_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-sent_at",)


class NotificationPreference(TenantScopedModel):
    """Per-user, per-org preferences.

    Publication and withdrawal notices are not switchable — PRD §6.9 makes a
    withdrawal something the client must always hear about.
    """

    org_user = models.ForeignKey(
        "portal.OrgUser", on_delete=models.CASCADE, related_name="notification_preferences"
    )
    key = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "org_user", "key"], name="uniq_pref_per_user_org"
            )
        ]
