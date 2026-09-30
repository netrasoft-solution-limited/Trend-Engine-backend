"""Operator sign-in, in a browser.

`apps/operations/api.py` already does all of this as JSON, for the React ops
client that does not exist yet. Meanwhile `LOGIN_URL` pointed at `/login`,
which was not a route — so `LoginRequiredMiddleware` redirected every operator
screen to a 404 and the provider-credentials page could not be reached at all.
A feature whose whole point was rotating keys without SSH needed SSH.

These views are the same three steps as the API, sharing one service layer in
`apps.operations.mfa`, so the two cannot drift on what a second factor means:

    /login      password        → pending state, never a session
    /mfa        code            → session, for an enrolled account
    /mfa/setup  scan + confirm  → enrolment, then a session

The pending state does the same work here as there. A correct password leaves
the operator anonymous to Django, so every other screen stays refused until a
code checks out.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from . import mfa
from .auth import OperatorBackend

logger = logging.getLogger(__name__)

public = method_decorator(login_not_required, name="dispatch")


def _home() -> str:
    """Where a signed-in operator lands.

    The Triage home (PRD §6.5) has no server-rendered view yet, so this points
    at the one operator screen that exists. It moves when Triage does.
    """
    return reverse("ops-providers")


@public
class LoginView(View):
    """Email and password. Never issues a session on its own."""

    def get(self, request):
        if request.user.is_authenticated:
            return redirect(_home())
        return render(request, "ops/login.html")

    def post(self, request):
        email = (request.POST.get("email") or "").strip().lower()
        password = request.POST.get("password") or ""

        bucket = f"ops-login:{email}"
        attempts = cache.get(bucket, 0)
        if attempts >= settings.OPERATOR_LOGIN_MAX_ATTEMPTS:
            return render(
                request,
                "ops/login.html",
                {"error": "Too many attempts. Try again shortly.", "email": email},
                status=429,
            )

        user = OperatorBackend().authenticate(request, email=email, password=password)
        if user is None:
            cache.set(bucket, attempts + 1, settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS)
            # One message for "no such account" and "wrong password".
            return render(
                request,
                "ops/login.html",
                {"error": "That email and password do not match.", "email": email},
                status=401,
            )

        cache.delete(bucket)

        if not settings.OPERATOR_REQUIRE_MFA:
            django_login(request, user, backend="apps.operations.auth.OperatorBackend")
            return redirect(_home())

        # The password is right, and that is all it buys.
        mfa.begin_pending(request.session, user)
        return redirect("ops-mfa" if user.mfa_ready else "ops-mfa-setup")


@public
class MfaView(View):
    """The second factor at sign-in: a TOTP code or a recovery code."""

    def get(self, request):
        user = mfa.pending_user(request.session)
        if user is None:
            return redirect("ops-login")
        if not user.mfa_ready:
            return redirect("ops-mfa-setup")
        return render(request, "ops/mfa.html", {"email": user.email})

    def post(self, request):
        user = mfa.pending_user(request.session)
        if user is None:
            return redirect("ops-login")

        bucket = f"ops-mfa:{user.pk}"
        attempts = cache.get(bucket, 0)
        if attempts >= settings.OPERATOR_MFA_MAX_ATTEMPTS:
            # Discard the pending state, so the ceiling is not a speed bump
            # someone waits out with the session still half open.
            mfa.clear_pending(request.session)
            return render(
                request,
                "ops/login.html",
                {"error": "Too many codes. Sign in with your password again."},
                status=429,
            )

        if not mfa.verify_code(user, request.POST.get("code") or ""):
            cache.set(bucket, attempts + 1, settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS)
            return render(
                request,
                "ops/mfa.html",
                # One message whether the code was wrong, expired or already
                # used: telling them apart tells an attacker their guess landed.
                {"email": user.email, "error": "That code is not right."},
                status=401,
            )

        cache.delete(bucket)
        return _complete(request, user)


@public
class MfaSetupView(View):
    """Enrolment, for an account signing in before it has an authenticator.

    Reachable only from a pending state, so it is not an endpoint that hands
    secrets to anonymous callers. An account that IS enrolled is sent to the
    challenge instead — re-enrolling here would let anyone with the password
    replace a working authenticator, which is most of what the factor is for.
    """

    def get(self, request):
        user = mfa.pending_user(request.session)
        if user is None:
            return redirect("ops-login")
        if user.mfa_ready:
            return redirect("ops-mfa")

        enrolment = mfa.begin_enrolment(user)
        # Held in the session so the POST verifies against the same secret the
        # operator just scanned, rather than issuing a new one on every render.
        return render(
            request,
            "ops/mfa_setup.html",
            {
                "email": user.email,
                "secret": enrolment.secret,
                "uri": enrolment.provisioning_uri,
                "qr": _qr_svg(enrolment.provisioning_uri),
            },
        )

    def post(self, request):
        user = mfa.pending_user(request.session)
        if user is None:
            return redirect("ops-login")

        try:
            codes = mfa.confirm_enrolment(user, request.POST.get("code") or "")
        except mfa.MfaError as exc:
            # Re-render WITHOUT a new secret: issuing one here would invalidate
            # what they just scanned because they mistyped six digits.
            from . import totp

            secret = totp.decrypt_secret(user.totp_secret)
            return render(
                request,
                "ops/mfa_setup.html",
                {
                    "email": user.email,
                    "secret": secret,
                    "uri": totp.provisioning_uri(secret, email=user.email),
                    "qr": _qr_svg(totp.provisioning_uri(secret, email=user.email)),
                    "error": str(exc),
                },
                status=400,
            )

        response = _complete(request, user)
        # The codes exist in readable form exactly once, so they are shown
        # rather than redirected past.
        return render(
            request,
            "ops/recovery_codes.html",
            {"codes": codes, "home": _home()},
        ) if codes else response


class LogoutView(View):
    def post(self, request):
        django_logout(request)
        return redirect("ops-login")


def _complete(request, user):
    """Turn a pending state into a real session."""
    mfa.clear_pending(request.session)
    django_login(request, user, backend="apps.operations.auth.OperatorBackend")
    return redirect(_home())


def _qr_svg(uri: str, *, scale: int = 5) -> str:
    """The provisioning URI as an inline SVG.

    Rendered from the raw module matrix rather than an image library: `qrcode`
    is pure Python, and inline SVG means no image file to serve, no static
    volume to populate, and nothing to configure in Caddy for one page.
    """
    import qrcode

    code = qrcode.QRCode(box_size=1, border=2)
    code.add_data(uri)
    code.make(fit=True)
    matrix = code.get_matrix()

    size = len(matrix) * scale
    rects = "".join(
        f'<rect x="{x * scale}" y="{y * scale}" width="{scale}" height="{scale}"/>'
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}" role="img" aria-label="Authenticator QR code">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<g fill="#000">{rects}</g></svg>'
    )
