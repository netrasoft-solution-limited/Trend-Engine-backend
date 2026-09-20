"""Tenant binding, one middleware per plane.

Two middlewares rather than one with a branch. Arch §5.3 and ADR #3 make
escalation structurally impossible rather than merely disallowed, and a single
middleware deciding which scope to bind is exactly the branch a misconfiguration
could get wrong. These are installed by different settings modules and loaded by
different WSGI processes, so the portal process never has the operator binder in
its stack at all.
"""
from __future__ import annotations

from .context import bind_operator_all, bind_tenant, mark_portal_plane, reset
from .exceptions import TenantScopeError


class OperatorTenantMiddleware:
    """Operator plane. Binds OPERATOR_ALL, narrowed by an explicit UI choice.

    Installed only by `config.settings.ops`.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        selected = self._selected_organization(request)
        token = bind_tenant(selected) if selected else bind_operator_all()
        try:
            return self.get_response(request)
        finally:
            reset(token)

    def _selected_organization(self, request):
        """The tenant chosen in the operator UI scope selector, if any.

        Returns None for operator-wide. Reads from the session rather than the
        query string so that a narrowed scope survives navigation.
        """
        organization_id = request.session.get("scope_organization_id")
        if not organization_id:
            return None

        from .models import Organization

        return Organization.objects.filter(pk=organization_id).first()


class PortalTenantMiddleware:
    """Tenant plane. Hard-binds the session's organization.

    Installed only by `config.settings.portal`.

    Note the order: `mark_portal_plane()` runs before anything else, so that
    even if operator code were somehow reachable in this process, its call to
    `bind_operator_all()` would raise rather than succeed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        mark_portal_plane()

        org_user = getattr(request, "user", None)
        organization = getattr(org_user, "organization", None)

        if organization is None:
            # Unauthenticated portal requests must not fall through to an
            # unbound context, where a missing @login_required would become a
            # cross-tenant read instead of a redirect.
            raise TenantScopeError("Portal request reached a view with no authenticated OrgUser")

        token = bind_tenant(organization)
        try:
            return self.get_response(request)
        finally:
            reset(token)
