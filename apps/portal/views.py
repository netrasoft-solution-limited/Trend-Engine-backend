"""The tenant plane's API — `/portal/api/*`.

Session-cookie authentication, not tokens: the cookie stays HttpOnly and
out of reach of script, and the React client never holds a credential it could
leak through XSS or localStorage.

AUTHORISATION IS DECIDED HERE, NOT IN REACT. `AdminOnly` in the frontend is a
convenience that hides a link; `IsOrgAdmin` below is the control. PRD §3.2 is
explicit that an Org Viewer never reaches user management or billing, and a
hidden link is not an access control.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.hashers import make_password
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import Http404
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.utils.text import slugify
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenancy.middleware import PORTAL_ORG_SESSION_KEY
from apps.tenancy.models import Organization

from . import serializers as s
from .auth import PortalBackend
from .models import (
    EmailVerificationToken,
    OrgInvite,
    OrgMembership,
    OrgRole,
    OrgUser,
    PortalLoginEvent,
)
from .notifications import send_invite_email, send_password_reset_email, send_verification_email
from .tokens import hash_invite_token, hash_token


class IsOrgAdmin(BasePermission):
    """Server-side role check, read from the membership the middleware bound.

    Never from a request parameter and never from anything the client sent.
    """

    message = "This action is available to organisation administrators."

    def has_permission(self, request, view) -> bool:
        return getattr(request, "org_role", None) == OrgRole.ADMIN


# `login_not_required` marks the view as exempt from LoginRequiredMiddleware so
# that DRF decides the outcome and returns JSON. Without it an unauthenticated
# API call would get a 302 to an HTML login page, which a fetch() cannot use.
api_public = method_decorator(login_not_required, name="dispatch")


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _record_login(request, *, email: str, outcome: str, user=None) -> None:
    """PRD §6.8 requires a portal login audit trail.

    Failures are recorded as well as successes — an audit that shows only
    successes cannot show you an attack in progress.
    """
    PortalLoginEvent.objects.create(
        org_user=user,
        email_attempted=email[:254],
        outcome=outcome,
        ip_address=_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
    )


@api_public
class CsrfView(APIView):
    """Hand the client a CSRF token.

    `CSRF_USE_SESSIONS = True` keeps the token in the session rather than a
    second cookie, so there is no `csrftoken` cookie for the React client to
    read. It fetches the token here once on boot and sends it back as
    `X-CSRFToken` on every unsafe request.

    Safe to expose: the token is only useful to a caller that also holds the
    session cookie, which an attacker's origin cannot read or send.

    `selfSignupEnabled` rides along because the client already calls this on
    boot, and it needs to know whether to offer the register page at all.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            {
                "csrfToken": get_token(request),
                "selfSignupEnabled": bool(settings.PORTAL_ALLOW_SELF_SIGNUP),
            }
        )


@api_public
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        from django.conf import settings

        form = s.LoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].strip().lower()
        password = form.validated_data["password"]

        # Throttle per address. Cheap, and it turns credential stuffing into a
        # slow job rather than a fast one.
        bucket = f"portal-login:{email}"
        attempts = cache.get(bucket, 0)
        if attempts >= settings.PORTAL_LOGIN_MAX_ATTEMPTS:
            _record_login(request, email=email, outcome=PortalLoginEvent.Outcome.RATE_LIMITED)
            return Response(
                {"detail": "Too many attempts. Try again shortly.", "code": "rate_limited"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = PortalBackend().authenticate(request, email=email, password=password)
        if user is None:
            cache.set(bucket, attempts + 1, settings.PORTAL_LOGIN_ATTEMPT_WINDOW_SECONDS)
            _record_login(request, email=email, outcome=PortalLoginEvent.Outcome.BAD_CREDENTIALS)
            # One message for "no such account" and "wrong password" — telling
            # them apart is an account-enumeration oracle.
            return Response(
                {"detail": "That email and password do not match.", "code": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        membership = user.active_memberships().first()
        if membership is None:
            _record_login(
                request, email=email, outcome=PortalLoginEvent.Outcome.NO_MEMBERSHIP, user=user
            )
            # Only reachable with the right password, so naming the reason
            # discloses nothing the caller does not already know — and it lets
            # the client offer "resend the verification email".
            if user.memberships.filter(status=OrgMembership.Status.PENDING_VERIFICATION).exists():
                return Response(
                    {
                        "detail": "Confirm your email address before signing in.",
                        "code": "email_not_verified",
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            return Response(
                {
                    "detail": "This account has no active organisation. Contact your administrator.",
                    "code": "no_active_membership",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        cache.delete(bucket)
        # Cycles the session key and rotates the CSRF token — session fixation
        # protection. Doing this by hand is one of the reasons not to roll your
        # own session handling.
        django_login(request, user, backend="apps.portal.auth.PortalBackend")
        request.session[PORTAL_ORG_SESSION_KEY] = membership.organization_id
        _record_login(request, email=email, outcome=PortalLoginEvent.Outcome.SUCCESS, user=user)

        membership.last_seen_at = timezone.now()
        membership.save(update_fields=["last_seen_at"])

        return Response(s.session_payload(user, membership))


class LogoutView(APIView):
    def post(self, request):
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionView(APIView):
    """Who am I, and in which organisation.

    The middleware has already bound the tenant and attached `membership`, so
    this view does not choose an organisation — it reports the one that was
    verified.
    """

    def get(self, request):
        return Response(s.session_payload(request.user, request.membership))


class SwitchOrgView(APIView):
    """Change the active organisation.

    Legitimate under multi-org membership, and bounded: the new organisation
    must be one the user holds an ACTIVE membership in. The check is here as
    well as in the middleware because writing an unverified id into the session
    would be handing the client a lever, even if the middleware later refuses
    it.
    """

    def post(self, request):
        form = s.SwitchOrgSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        target = form.validated_data["organization_id"]

        membership = request.user.membership_for(target)
        if membership is None:
            return Response(
                {"detail": "No active membership for that organisation.", "code": "no_membership"},
                status=status.HTTP_403_FORBIDDEN,
            )

        request.session[PORTAL_ORG_SESSION_KEY] = membership.organization_id
        return Response(s.session_payload(request.user, membership))


@api_public
class PasswordResetRequestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        form = s.PasswordResetRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].strip().lower()

        user = OrgUser.objects.filter(email=email, is_active=True).first()
        if user is not None:
            send_password_reset_email(
                user,
                uid=urlsafe_base64_encode(force_bytes(user.pk)),
                token=default_token_generator.make_token(user),
            )

        # Always the same response, whether or not the address exists.
        return Response({"detail": "If that address has an account, a reset link is on its way."})


@api_public
class PasswordResetConfirmView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        form = s.PasswordResetConfirmSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            uid = force_str(urlsafe_base64_decode(form.validated_data["uid"]))
            user = OrgUser.objects.get(pk=uid, is_active=True)
        except (OrgUser.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response(
                {"detail": "That reset link is no longer valid.", "code": "invalid_token"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not default_token_generator.check_token(user, form.validated_data["token"]):
            return Response(
                {"detail": "That reset link is no longer valid.", "code": "invalid_token"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(form.validated_data["password"])
        user.save(update_fields=["password"])

        # Changing the password must end every other session. The token in each
        # of those sessions is derived from the password hash, so rotating the
        # hash invalidates them — but only if we refresh THIS one, or the user
        # logs themselves out by resetting.
        if request.user.is_authenticated and request.user.pk == user.pk:
            update_session_auth_hash(request, user)

        return Response({"detail": "Password updated. You can sign in now."})


@api_public
class InviteAcceptView(APIView):
    """Accept an invitation.

    Public by necessity — the invitee has no account yet. The token is the
    credential, so it is single-use, time-limited, and stored only as a hash.
    """

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        form = s.InviteAcceptSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        invite = (
            OrgInvite.objects.select_for_update()
            .filter(token_hash=hash_invite_token(form.validated_data["token"]))
            .select_related("organization")
            .first()
        )
        if invite is None or not invite.is_usable:
            return Response(
                {"detail": "That invitation is no longer valid.", "code": "invalid_invite"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = OrgUser.objects.filter(email=invite.email.lower()).first()
        if user is None:
            user = OrgUser.objects.create_user(
                email=invite.email,
                password=form.validated_data["password"],
                name=form.validated_data.get("name") or invite.email.split("@")[0],
            )
        elif not user.has_usable_password():
            # Invited earlier, never completed. Let them finish.
            user.set_password(form.validated_data["password"])
            user.save(update_fields=["password"])

        membership, _ = OrgMembership.objects.update_or_create(
            org_user=user,
            organization=invite.organization,
            defaults={
                "role": invite.role,
                "status": OrgMembership.Status.ACTIVE,
                "accepted_at": timezone.now(),
                "invited_by": invite.invited_by,
            },
        )

        invite.accepted_at = timezone.now()
        invite.save(update_fields=["accepted_at"])

        return Response(
            {"detail": "Invitation accepted.", "organization": invite.organization.name},
            status=status.HTTP_201_CREATED,
        )


# ── Self-service registration ───────────────────────────────────────────────
# A recorded deviation from PRD §6.8 (see README.md). A new organisation is
# created ONBOARDING with its admin's membership PENDING_VERIFICATION; the admin
# cannot log in until they prove they own the email address, and verifying
# activates both. Joining an existing organisation remains invite-only.
#
# register and resend answer identically whether or not the address is known.
# A different status, body or noticeably different latency for "already has an
# account" would be an account-enumeration oracle on a public endpoint.

_REGISTER_ACCEPTED = {
    "detail": "Check your inbox. If this address can be registered, "
    "we have sent a link to confirm it.",
}
_RESEND_ACCEPTED = {
    "detail": "If this address has a registration waiting to be confirmed, "
    "a new link is on its way.",
}
_RATE_LIMITED = {"detail": "Too many attempts. Try again later.", "code": "rate_limited"}


def _throttle(*buckets: tuple[str, int], window: int) -> bool:
    """Count this attempt in every bucket; True if any bucket is over its limit.

    The same cache-bucket idea as LoginView, but `add` + `incr` rather than
    get-then-set, so concurrent requests cannot both read the same count.
    """
    over = False
    for key, limit in buckets:
        cache.add(key, 0, window)
        try:
            count = cache.incr(key)
        except ValueError:  # expired between add() and incr()
            cache.set(key, 1, window)
            count = 1
        over = over or count > limit
    return over


def _create_onboarding_organization(name: str) -> Organization:
    """A new ONBOARDING organisation with a unique slug derived from its name."""
    base = slugify(name)[:40].strip("-") or "org"
    candidates = [base, *(f"{base}-{n}" for n in range(2, 11)), f"{base}-{secrets.token_hex(4)}"]
    for slug in candidates:
        if Organization.objects.filter(slug=slug).exists():
            continue
        try:
            # Savepoint: losing a race for this slug must not poison the
            # enclosing registration transaction.
            with transaction.atomic():
                return Organization.objects.create(
                    slug=slug, name=name, status=Organization.Status.ONBOARDING
                )
        except IntegrityError:
            continue
    raise IntegrityError(f"Could not allocate an organisation slug for {name!r}")


def _issue_verification_token(membership: OrgMembership) -> str:
    """Store the hash, return the raw token. The raw value is never persisted."""
    raw = EmailVerificationToken.new_token()
    EmailVerificationToken.objects.create(membership=membership, token_hash=hash_token(raw))
    return raw


def _send_after_commit(membership: OrgMembership, raw_token: str) -> None:
    # After commit, so a rolled-back registration never leaves a live link in
    # someone's inbox. `robust`: a mail failure is logged rather than turned
    # into a 500 — a 500 on this path only would itself reveal that the
    # address was new. The user can ask for a resend.
    transaction.on_commit(
        lambda: send_verification_email(membership, raw_token=raw_token), robust=True
    )


@api_public
class RegisterView(APIView):
    """POST auth/register — create a new organisation and its admin."""

    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.PORTAL_ALLOW_SELF_SIGNUP:
            raise Http404

        form = s.RegisterSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        email = data["email"].strip().lower()
        ip = _client_ip(request) or "unknown"

        if _throttle(
            (f"portal-register:email:{email}", settings.PORTAL_SIGNUP_MAX_PER_EMAIL),
            (f"portal-register:ip:{ip}", settings.PORTAL_SIGNUP_MAX_PER_IP),
            window=settings.PORTAL_SIGNUP_WINDOW_SECONDS,
        ):
            _record_login(request, email=email, outcome=PortalLoginEvent.Outcome.RATE_LIMITED)
            return Response(_RATE_LIMITED, status=status.HTTP_429_TOO_MANY_REQUESTS)

        if OrgUser.objects.filter(email=email).exists():
            # Create nothing, reveal nothing. The hash is the expensive part of
            # the real path; doing it here keeps the two paths' latency close.
            make_password(data["password"])
            return Response(_REGISTER_ACCEPTED, status=status.HTTP_202_ACCEPTED)

        try:
            with transaction.atomic():
                organization = _create_onboarding_organization(data["organization_name"])
                user = OrgUser.objects.create_user(
                    email=email, password=data["password"], name=data["name"]
                )
                membership = OrgMembership.objects.create(
                    org_user=user,
                    organization=organization,
                    # Always ADMIN. Never read from the request.
                    role=OrgRole.ADMIN,
                    status=OrgMembership.Status.PENDING_VERIFICATION,
                )
                raw = _issue_verification_token(membership)
                _record_login(
                    request, email=email, outcome=PortalLoginEvent.Outcome.REGISTERED, user=user
                )
                _send_after_commit(membership, raw)
        except IntegrityError:
            # A concurrent registration took this email between the check and
            # the insert. Same answer as any other already-registered address.
            pass

        return Response(_REGISTER_ACCEPTED, status=status.HTTP_202_ACCEPTED)


@api_public
class VerifyEmailView(APIView):
    """POST auth/verify — activate the organisation and its admin.

    Does NOT log the user in. Proving control of an inbox is not the same as
    presenting the password, and the client goes to the login screen next.
    """

    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        form = s.VerifyEmailSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        token = (
            EmailVerificationToken.objects.select_for_update()
            .filter(token_hash=hash_token(form.validated_data["token"]))
            .first()
        )
        membership = None
        if token is not None and token.is_usable:
            membership = (
                OrgMembership.objects.select_for_update()
                .filter(pk=token.membership_id, status=OrgMembership.Status.PENDING_VERIFICATION)
                .select_related("org_user")
                .first()
            )

        if membership is None:
            user = token.membership.org_user if token is not None else None
            _record_login(
                request,
                email=user.email if user else "",
                outcome=PortalLoginEvent.Outcome.VERIFY_FAILED,
                user=user,
            )
            return Response(
                {"detail": "That confirmation link is no longer valid.", "code": "invalid_token"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        organization = Organization.objects.select_for_update().get(pk=membership.organization_id)
        # Only ONBOARDING moves. An organisation an operator has suspended in
        # the meantime stays suspended.
        if organization.status == Organization.Status.ONBOARDING:
            organization.status = Organization.Status.ACTIVE
            organization.save(update_fields=["status"])

        membership.status = OrgMembership.Status.ACTIVE
        membership.accepted_at = now
        membership.save(update_fields=["status", "accepted_at"])

        token.used_at = now
        token.save(update_fields=["used_at"])
        EmailVerificationToken.objects.filter(
            membership=membership, used_at__isnull=True, invalidated_at__isnull=True
        ).update(invalidated_at=now)

        _record_login(
            request,
            email=membership.org_user.email,
            outcome=PortalLoginEvent.Outcome.VERIFIED,
            user=membership.org_user,
        )
        return Response(
            {"detail": "Email confirmed. You can sign in now.", "organization": organization.name}
        )


@api_public
class ResendVerificationView(APIView):
    """POST auth/verify/resend — a fresh link; every earlier unused one dies."""

    permission_classes = [AllowAny]

    def post(self, request):
        form = s.ResendVerificationSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].strip().lower()
        ip = _client_ip(request) or "unknown"

        if _throttle(
            (f"portal-verify-resend:email:{email}", settings.PORTAL_VERIFY_RESEND_MAX_PER_EMAIL),
            (f"portal-verify-resend:ip:{ip}", settings.PORTAL_VERIFY_RESEND_MAX_PER_IP),
            window=settings.PORTAL_SIGNUP_WINDOW_SECONDS,
        ):
            _record_login(request, email=email, outcome=PortalLoginEvent.Outcome.RATE_LIMITED)
            return Response(_RATE_LIMITED, status=status.HTTP_429_TOO_MANY_REQUESTS)

        with transaction.atomic():
            membership = (
                OrgMembership.objects.select_for_update(of=("self",))
                .filter(org_user__email=email, status=OrgMembership.Status.PENDING_VERIFICATION)
                .select_related("org_user", "organization")
                .first()
            )
            if membership is not None:
                EmailVerificationToken.objects.filter(
                    membership=membership, used_at__isnull=True, invalidated_at__isnull=True
                ).update(invalidated_at=timezone.now())
                _send_after_commit(membership, _issue_verification_token(membership))

        return Response(_RESEND_ACCEPTED, status=status.HTTP_202_ACCEPTED)


class TeamView(APIView):
    """Org Admin only, enforced server-side.

    Members are read through `OrgMembership.scoped`, the tenant-scoped manager,
    so the active organisation comes from the bound context rather than from a
    filter this view could forget.
    """

    permission_classes = [IsOrgAdmin]

    def get(self, request):
        members = s.TeamMemberSerializer(
            OrgMembership.scoped.select_related("org_user").all(), many=True
        ).data
        invites = s.PendingInviteSerializer(
            OrgInvite.objects.filter(
                organization=request.organization, accepted_at__isnull=True, revoked_at__isnull=True
            ),
            many=True,
        ).data
        return Response({"members": members, "invites": invites})

    def post(self, request):
        form = s.InviteCreateSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].strip().lower()

        if OrgMembership.scoped.filter(org_user__email=email).exists():
            return Response(
                {"detail": "That person is already on this team.", "code": "already_member"},
                status=status.HTTP_409_CONFLICT,
            )

        raw = OrgInvite.new_token()
        invite = OrgInvite.objects.create(
            organization=request.organization,
            email=email,
            role=form.validated_data["role"],
            token_hash=hash_invite_token(raw),
            invited_by=request.user,
        )
        send_invite_email(invite, raw_token=raw)

        return Response(s.PendingInviteSerializer(invite).data, status=status.HTTP_201_CREATED)


class TeamMemberView(APIView):
    permission_classes = [IsOrgAdmin]

    def delete(self, request, membership_id: int):
        membership = OrgMembership.scoped.filter(pk=membership_id).first()
        if membership is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        if membership.org_user_id == request.user.pk:
            return Response(
                {"detail": "You cannot remove your own access.", "code": "self_removal"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        admins = OrgMembership.scoped.filter(
            role=OrgRole.ADMIN, status=OrgMembership.Status.ACTIVE
        ).count()
        if membership.role == OrgRole.ADMIN and admins <= 1:
            return Response(
                {
                    "detail": "This organisation would be left with no administrator.",
                    "code": "last_admin",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        membership.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
