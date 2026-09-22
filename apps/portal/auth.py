"""The tenant realm's authentication backend.

`config.settings.portal` lists this and nothing else in
`AUTHENTICATION_BACKENDS`. That matters more than it looks: Django's
`get_user()` refuses any session whose `_auth_user_backend` is not in the
active process's backend list. So even if an operator-authenticated session
cookie were somehow presented to the portal process, it would degrade to
`AnonymousUser` rather than resolving an operator — a structural guard we get
by using contrib.auth natively rather than rolling our own session handling.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password

from .models import OrgUser


class PortalBackend(BaseBackend):
    """Email + password, for `portal.OrgUser` only.

    Deliberately does NOT check membership. A user with no active membership
    authenticates successfully and is then refused by
    `PortalTenantMiddleware`, which flushes the session and returns 401. Two
    reasons to split it that way:

      · the login view wants to distinguish "wrong password" from "no longer
        has access", and record them differently in PortalLoginEvent;
      · membership can be revoked mid-session, so it has to be re-checked per
        request anyway. Checking it only at login would be the weaker guard.
    """

    def authenticate(self, request, email: str | None = None, password: str | None = None, **kwargs):
        if email is None:
            email = kwargs.get("username")
        if not email or not password:
            return None

        try:
            user = OrgUser.objects.get(email=email.strip().lower())
        except OrgUser.DoesNotExist:
            # Hash anyway. Returning early on an unknown address leaks which
            # addresses exist through response timing.
            OrgUser().set_password(password)
            return None

        if not user.check_password(password):
            return None
        if not user.is_active:
            return None
        return user

    def get_user(self, user_id):
        try:
            return OrgUser.objects.get(pk=user_id)
        except OrgUser.DoesNotExist:
            return None

    def user_can_authenticate(self, user) -> bool:
        return bool(getattr(user, "is_active", False))


def verify_password(raw: str, encoded: str) -> bool:
    """Exposed for the invite-acceptance flow, which sets a password before any
    session exists."""
    return check_password(raw, encoded)
