"""The bound tenant, as request-local state.

A contextvar rather than thread-local: Celery tasks and any future async views
need the binding to travel with the logical task, not the OS thread.

Three states, and the distinction between the first two is the whole design:

    UNSET         nothing bound — every tenant-scoped query raises
    OPERATOR_ALL  explicit, audited opt-out — operator plane only
    <Organization> bound to exactly one tenant

Arch §5.2: "Unscoped access is an exception, not a silent full-table scan."

On the plane check
-----------------
`bind_operator_all()` refuses on the tenant plane. That refusal is driven by
`settings.TENANT_PLANE`, which is a property of the PROCESS — set once by
`config.settings.portal` — not of the request.

An earlier version marked the plane from middleware. That made the guarantee
conditional on one middleware having run, so a Celery task, a management
command run inside the portal container, or a request short-circuited by an
earlier middleware all saw an unmarked context and could bind operator scope.
The contextvar is kept as a second layer for tests, and it is now reset.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Any, Iterator

from django.conf import settings

from .exceptions import TenantEscalationError


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self._name


#: No tenant bound. The default, deliberately — forgetting to scope must fail.
UNSET = _Sentinel("UNSET")

#: Operator plane opt-out. Never a default, never reachable from the portal.
OPERATOR_ALL = _Sentinel("OPERATOR_ALL")

_tenant: ContextVar[Any] = ContextVar("trend_engine_tenant", default=UNSET)

#: Second layer, for tests and for defence in depth. The primary check is
#: `settings.TENANT_PLANE`.
_portal_plane: ContextVar[bool] = ContextVar("trend_engine_portal_plane", default=False)


def is_portal_plane() -> bool:
    """True when this process — or this context — is the tenant plane."""
    if getattr(settings, "TENANT_PLANE", "operator") == "portal":
        return True
    return _portal_plane.get()


def current_tenant() -> Any:
    """Return the bound tenant, OPERATOR_ALL, or UNSET."""
    return _tenant.get()


def bind_tenant(organization: Any) -> Any:
    """Bind a single organization. Returns a token for `reset`."""
    return _tenant.set(organization)


def bind_operator_all() -> Any:
    """Bind the operator-wide scope.

    Refuses on the tenant plane. Arch §5.3 makes escalation structurally
    impossible rather than merely disallowed, and this is where that promise is
    kept in code: the portal process declares its plane in settings, and this
    function has no argument that can override it.
    """
    if is_portal_plane():
        raise TenantEscalationError(
            "The portal plane cannot bind OPERATOR_ALL. "
            "If you are seeing this, a portal request or task reached operator code."
        )
    return _tenant.set(OPERATOR_ALL)


@contextlib.contextmanager
def portal_plane() -> Iterator[None]:
    """Mark this context as portal-side for the duration of the block.

    For tests and for any operator-process code that wants to prove it behaves
    correctly under portal constraints. Resets on exit — an earlier version did
    not, which leaked the flag across requests on a threaded worker and made
    the test suite order-dependent.
    """
    token = _portal_plane.set(True)
    try:
        yield
    finally:
        _portal_plane.reset(token)


def reset(token: Any) -> None:
    _tenant.reset(token)


@contextlib.contextmanager
def scoped(organization: Any) -> Iterator[None]:
    """Run a block bound to one organization.

    The form Celery tasks should use, since a task has no middleware to bind
    for it and an unbound task would otherwise raise on its first query.
    """
    token = bind_tenant(organization)
    try:
        yield
    finally:
        reset(token)


@contextlib.contextmanager
def operator_scope() -> Iterator[None]:
    """Run a block with operator-wide visibility.

    Deliberately verbose to type and trivial to grep for in review. Every use
    is a decision to read across tenants.
    """
    token = bind_operator_all()
    try:
        yield
    finally:
        reset(token)
