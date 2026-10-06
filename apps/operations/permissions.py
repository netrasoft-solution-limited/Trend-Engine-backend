"""Role checks for operator screens.

`apps.operations` is excluded from the layers contract because it is
cross-cutting — the cost ledger and the audit log are written by everything —
so a role check can live here and be imported by any screen without inverting
a dependency.

It is here rather than copied per app because it was already copied once.
`apps/sources/views.py` grew a private `_require_platform_admin`, and the next
screen would have grown a second one; two definitions of "who may do this"
drift, and the drift is silent until the wrong person can do something.

A function rather than a mixin, matching how the existing screens are written:
`View` subclasses with the check as the first statement of each handler, so it
is visible in the handler rather than inherited from somewhere above.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied

from .models import OperatorUser


def is_platform_admin(user) -> bool:
    """`is_superuser` short-circuits, so a bootstrapped superuser is never
    locked out of the screen that would let them fix the roles."""
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "role", "") == OperatorUser.Role.PLATFORM_ADMIN
    )


def require_platform_admin(request, *, action: str = "This") -> None:
    """Raise unless the signed-in operator holds Platform Admin.

    PRD §3.2 keeps Operator and Platform Admin distinct even where one person
    holds both, so that separating them later needs no schema change. `action`
    names what was refused — a 403 that does not say what it refused sends the
    reader to the logs.
    """
    if not is_platform_admin(request.user):
        raise PermissionDenied(f"{action} requires the Platform Admin role.")
