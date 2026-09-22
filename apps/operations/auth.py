"""The operator realm's authentication backend.

The mirror image of `apps.portal.auth.PortalBackend`, and listed alone in
`config.settings.ops`'s `AUTHENTICATION_BACKENDS` for the same reason: a portal
session presented to the operator process cannot resolve a user, because the
backend that issued it is not registered here.
"""
from __future__ import annotations

from django.contrib.auth.backends import BaseBackend

from .models import OperatorUser


class OperatorBackend(BaseBackend):
    """Email + password for `operations.OperatorUser`.

    PRD §7.1 requires MFA where supported. `OPERATOR_REQUIRE_MFA` is set in
    `config.settings.ops`; enforcing the second factor belongs in the login
    view, not here, so that a partially-authenticated state never becomes a
    usable session.
    """

    def authenticate(self, request, email: str | None = None, password: str | None = None, **kwargs):
        if email is None:
            email = kwargs.get("username")
        if not email or not password:
            return None

        try:
            user = OperatorUser.objects.get(email=email.strip().lower())
        except OperatorUser.DoesNotExist:
            # Constant-ish time: do the hash work even when the address is
            # unknown, so timing does not enumerate operator accounts.
            OperatorUser().set_password(password)
            return None

        if not user.check_password(password) or not user.is_active:
            return None
        return user

    def get_user(self, user_id):
        try:
            return OperatorUser.objects.get(pk=user_id)
        except OperatorUser.DoesNotExist:
            return None

    def user_can_authenticate(self, user) -> bool:
        return bool(getattr(user, "is_active", False))
