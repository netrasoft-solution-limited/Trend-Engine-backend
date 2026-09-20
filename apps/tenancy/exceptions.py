"""Tenancy failures.

Arch §13: a TenantScopeError reaching production is a P1. It means a code path
tried to read tenant data with no tenant bound — which in a system without this
guard would have been a silent full-table scan across every client.
"""


class TenantScopeError(RuntimeError):
    """Raised when a tenant-scoped model is queried with no tenant bound."""


class TenantEscalationError(RuntimeError):
    """Raised when the portal plane attempts to widen its own scope.

    Arch §5.3: there is no legitimate code path for this. If it is ever raised,
    treat it as an attempted privilege escalation, not a bug.
    """
