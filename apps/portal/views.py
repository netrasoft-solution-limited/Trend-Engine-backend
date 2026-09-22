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

from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenancy.middleware import PORTAL_ORG_SESSION_KEY

from . import serializers as s
from .auth import PortalBackend
from .models import OrgInvite, OrgMembership, OrgRole, OrgUser, PortalLoginEvent
from .notifications import send_invite_email, send_password_reset_email
from .tokens import hash_invite_token


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
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


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
