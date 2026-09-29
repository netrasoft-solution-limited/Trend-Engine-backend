"""The operator plane's JSON auth API — `/ops/api/auth/*`.

The Triage endpoints live with the Triage data, in `apps.scoring.api`; this
app may not import `scoring` (see apps/operations/models.py).

The same shape as the portal's (`apps/portal/views.py`): session-cookie
authentication, a CSRF endpoint the client calls on boot, DRF `APIView`s, and
default-deny routing, so every view here needs a signed-in operator unless it
is marked `api_public`.

MFA. PRD §7.1 requires a second factor for operators, and it is not built yet.
While `OPERATOR_REQUIRE_MFA` is True — always, in production — login checks
the password and then refuses with `mfa_not_implemented`, creating no session.
Local development turns the setting off (see config/settings/ops.py); login
then succeeds and says `"mfa_required": false`.
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
            # The password was right, but there is no second factor to ask for
            # yet. Refuse outright — never a password-only session.
            return Response(
                {
                    "detail": "Operator sign-in needs a second factor, which is not "
                    "available yet. Operator login is disabled until it is.",
                    "code": "mfa_not_implemented",
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Cycles the session key and rotates the CSRF token.
        django_login(request, user, backend="apps.operations.auth.OperatorBackend")
        return Response({**s.session_payload(user), "mfa_required": False})


class LogoutView(APIView):
    def post(self, request):
        django_logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class SessionView(APIView):
    """Who am I. 401 when signed out."""

    def get(self, request):
        return Response(s.session_payload(request.user))
