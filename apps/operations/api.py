"""The operator plane's JSON auth API — `ops.<domain>/api/auth/*`.

The Triage endpoints live with the Triage data, in `apps.scoring.api`; this
app may not import `scoring` (see apps/operations/models.py).

The same shape as the portal's (`apps/portal/views.py`): session-cookie
authentication, a CSRF endpoint the client calls on boot, DRF `APIView`s, and
default-deny routing, so every view here needs a signed-in operator unless it
is marked `api_public`.

MFA. PRD §7.1 requires a second factor for operators. `OPERATOR_REQUIRE_MFA`
is True in production always, and a correct password alone now buys a PENDING
state rather than a refusal or a session — see `apps.operations.mfa` for why a
partially authenticated session is just an authenticated session with a comment
on it. The operator stays anonymous to Django until a code checks out, so
`LoginRequiredMiddleware` keeps refusing every other view in the meantime.

Login answers with a `code` saying what to do next:

    mfa_challenge      enrolled; POST the code to auth/mfa/verify
    mfa_setup_required not enrolled; GET auth/mfa/setup, then auth/mfa/confirm

Local development turns the setting off (see config/settings/ops.py); login
then succeeds outright and says `"mfa_required": false`.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import mfa
from . import serializers as s
from .auth import OperatorBackend

# See the same decorator in apps/portal/views.py: exempt from
# LoginRequiredMiddleware so DRF, not a 302 to an HTML page, decides the answer.
api_public = method_decorator(login_not_required, name="dispatch")


@api_public
class CsrfView(APIView):
    """Hand the client a CSRF token. `CSRF_USE_SESSIONS` keeps it in the
    session, so the client sends it back as `X-CSRFToken`."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})


@api_public
class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        form = s.LoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        email = form.validated_data["email"].strip().lower()

        bucket = f"ops-login:{email}"
        attempts = cache.get(bucket, 0)
        if attempts >= settings.OPERATOR_LOGIN_MAX_ATTEMPTS:
            return Response(
                {"detail": "Too many attempts. Try again shortly.", "code": "rate_limited"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        user = OperatorBackend().authenticate(
            request, email=email, password=form.validated_data["password"]
        )
        if user is None:
            cache.set(bucket, attempts + 1, settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS)
            # One message for "no such account" and "wrong password".
            return Response(
                {"detail": "That email and password do not match.", "code": "invalid_credentials"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        cache.delete(bucket)

        if settings.OPERATOR_REQUIRE_MFA:
            # The password is right, and that is ALL it buys. No session is
            # created here; the operator is still anonymous to Django.
            mfa.begin_pending(request.session, user)
            return Response(
                {
                    "mfa_required": True,
                    "code": "mfa_challenge" if user.mfa_ready else "mfa_setup_required",
                    "detail": (
                        "Enter the code from your authenticator."
                        if user.mfa_ready
                        else "This account has no second factor yet. Set one up to continue."
                    ),
                }
            )

        # Cycles the session key and rotates the CSRF token.
        django_login(request, user, backend="apps.operations.auth.OperatorBackend")
        return Response({**s.session_payload(user), "mfa_required": False})


def _complete_login(request, user) -> Response:
    """Turn a pending state into a real session, once a factor has checked out."""
    mfa.clear_pending(request.session)
    django_login(request, user, backend="apps.operations.auth.OperatorBackend")
    return Response({**s.session_payload(user), "mfa_required": False})


def _pending_or_401(request):
    user = mfa.pending_user(request.session)
    if user is None:
        return None, Response(
            {
                "detail": "Sign in with your password first.",
                "code": "no_pending_login",
            },
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return user, None


@api_public
class MfaSetupView(APIView):
    """The secret to scan, for an account part-way through its first sign-in.

    Reachable only from a pending state — a correct password — so it is not an
    endpoint that hands secrets to anonymous callers.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        user, refusal = _pending_or_401(request)
        if refusal:
            return refusal

        if user.mfa_ready:
            # Re-enrolling from here would let anyone with the password replace
            # a working authenticator. That is a reset, and it is deliberately
            # not on the web: `manage.py operator_mfa --reset`.
            return Response(
                {
                    "detail": "This account already has a second factor.",
                    "code": "already_enrolled",
                },
                status=status.HTTP_409_CONFLICT,
            )

        enrolment = mfa.begin_enrolment(user)
        return Response(
            {
                "provisioning_uri": enrolment.provisioning_uri,
                # For an app that cannot scan. Shown once, alongside the QR.
                "secret": enrolment.secret,
                "email": user.email,
            }
        )


@api_public
class MfaConfirmView(APIView):
    """Prove the authenticator reads the secret, then sign in.

    Returns the recovery codes ONCE — they are stored hashed, so this is the
    only moment they exist in readable form.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        user, refusal = _pending_or_401(request)
        if refusal:
            return refusal

        form = s.MfaCodeSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            codes = mfa.confirm_enrolment(user, form.validated_data["code"])
        except mfa.MfaError as exc:
            return Response(
                {"detail": str(exc), "code": "invalid_code"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        response = _complete_login(request, user)
        response.data["recovery_codes"] = codes
        return response


@api_public
class MfaVerifyView(APIView):
    """The second factor at sign-in. A TOTP code or a recovery code."""

    permission_classes = [AllowAny]

    def post(self, request):
        user, refusal = _pending_or_401(request)
        if refusal:
            return refusal

        form = s.MfaCodeSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        # Rate-limited separately from the password. Without this the second
        # factor is six digits standing alone against unlimited guesses.
        bucket = f"ops-mfa:{user.pk}"
        attempts = cache.get(bucket, 0)
        if attempts >= settings.OPERATOR_MFA_MAX_ATTEMPTS:
            mfa.clear_pending(request.session)
            return Response(
                {
                    "detail": "Too many codes. Sign in with your password again.",
                    "code": "rate_limited",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        if not user.mfa_ready:
            return Response(
                {"detail": "This account has no second factor yet.", "code": "mfa_setup_required"},
                status=status.HTTP_409_CONFLICT,
            )

        if not mfa.verify_code(user, form.validated_data["code"]):
            cache.set(bucket, attempts + 1, settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS)
            # One message whether the code was wrong, expired or already used:
            # telling them apart tells an attacker their guess was right.
            return Response(
                {"detail": "That code is not right.", "code": "invalid_code"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        cache.delete(bucket)
        return _complete_login(request, user)


class LogoutView(APIView):
    def post(self, request):
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionView(APIView):
    """Who am I. 401 when signed out."""

    def get(self, request):
        return Response(s.session_payload(request.user))
