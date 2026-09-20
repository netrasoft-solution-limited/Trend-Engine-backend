"""The bound tenant, as request-local state.

A contextvar rather than thread-local: Celery tasks and any future async views
need the binding to travel with the logical task, not the OS thread.

Three states, and the distinction between the first two is the whole design:

    UNSET         nothing bound — every tenant-scoped query raises
    OPERATOR_ALL  explicit, audited opt-out — operator plane only
    <Organization> bound to exactly one tenant

Arch §5.2: "Unscoped access is an exception, not a silent full-table scan."
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Any, Iterator

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

#: Set once by portal middleware. While true, OPERATOR_ALL cannot be bound.
_portal_plane: ContextVar[bool] = ContextVar("trend_engine_portal_plane", default=False)


def current_tenant() -> Any:
    """Return the bound tenant, OPERATOR_ALL, or UNSET."""
    return _tenant.get()


def bind_tenant(organization: Any) -> Any:
    """Bind a single organization. Returns a token for `reset`."""
    return _tenant.set(organization)


def bind_operator_all() -> Any:
    """Bind the operator-wide scope.

    Refuses on the portal plane. Arch §5.3 makes escalation structurally
    impossible rather than merely disallowed, and this is where that promise is
    kept in code: the portal's WSGI process marks itself, and this function has
    no argument that can unmark it.
    """
    if _portal_plane.get():
        raise TenantEscalationError(
            "The portal plane cannot bind OPERATOR_ALL. "
            "If you are seeing this, a portal request reached operator code."
        )
    return _tenant.set(OPERATOR_ALL)


def mark_portal_plane() -> None:
    """Mark this context as portal-side. One-way for the life of the request."""
    _portal_plane.set(True)


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
