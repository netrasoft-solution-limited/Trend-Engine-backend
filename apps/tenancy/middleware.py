"""Tenant binding, one middleware per plane.

Two middlewares rather than one with a branch. Arch §5.3 and ADR #3 make
escalation structurally impossible rather than merely disallowed, and a single
middleware deciding which scope to bind is exactly the branch a
misconfiguration could get wrong. These are installed by different settings
modules and loaded by different WSGI processes, so the portal process never has
the operator binder in its stack at all.
"""
from __future__ import annotations

import logging

from django.http import JsonResponse

from .context import bind_operator_all, bind_tenant, reset

logger = logging.getLogger(__name__)

#: Session key holding the organisation the portal user is currently acting in.
#: A CANDIDATE, never an authority — see PortalTenantMiddleware.
PORTAL_ORG_SESSION_KEY = "portal_org_id"

#: Operator-side scope narrowing. Safe to read from the session because the
#: DEFAULT is OPERATOR_ALL, so a session value can only ever de-escalate.
OPERATOR_SCOPE_SESSION_KEY = "scope_organization_id"


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

        Reading this from the session is safe HERE and would not be on the
        portal: the operator default is OPERATOR_ALL, so a tampered value can
        only narrow what the operator sees. The portal's default is nothing, so
        a tampered value there would WIDEN it — which is why the portal
        re-checks membership instead of trusting the session.
        """
        organization_id = request.session.get(OPERATOR_SCOPE_SESSION_KEY)
        if not organization_id:
            return None

        from .models import Organization

        return Organization.objects.filter(pk=organization_id).first()


class PortalTenantMiddleware:
    """Tenant plane. Binds an organisation the authenticated user belongs to.

    Installed only by `config.settings.portal`.

    Three rules, and the first two used to be wrong:

    1. ANONYMOUS REQUESTS BIND NOTHING AND DO NOT RAISE.
       An earlier version raised TenantScopeError here, reasoning that an
       unbound context would turn a missing @login_required into a cross-tenant
       read. That is backwards. `TenantScopedManager.get_queryset()` RAISES
       when nothing is bound — UNSET is fail-closed, and is the entire point of
       the sentinel. Not binding is safe; manufacturing an exception to mean
       "not logged in" turned every logged-out visit into a 500 instead of a
       redirect to the login page.

    2. THE SESSION'S ORGANISATION IS A CANDIDATE, NOT AN AUTHORITY.
       Multi-org membership means the active organisation has to travel
       somewhere, and it travels in the session — which the client can tamper
       with. So it is re-verified against OrgMembership on EVERY request. A
       forged id resolves to no membership and the request is refused. This
       check is the whole reason multi-org is safe; it is the thing to protect
       in review.

    3. AN INVARIANT VIOLATION IS NOT A ROUTING OUTCOME.
       Authenticated but with no usable membership — suspended, removed,
       organisation deactivated — flushes the session and refuses. It does not
       silently fall through unbound, and it does not 500.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        # Rule 1: anonymous binds nothing. LoginRequiredMiddleware (or the
        # view) decides what the caller sees.
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        membership = self._resolve_membership(request, user)
        if membership is None:
            return self._refuse(request, user)

        request.membership = membership
        request.organization = membership.organization
        request.org_role = membership.role

        token = bind_tenant(membership.organization)
        try:
            return self.get_response(request)
        finally:
            reset(token)

    def _resolve_membership(self, request, user):
        """Rule 2. Never trust the session id on its own."""
        candidate = request.session.get(PORTAL_ORG_SESSION_KEY)

        if candidate is not None:
            membership = user.membership_for(candidate)
            if membership is not None:
                return membership
            # Forged, stale, or revoked. Fall through to the default rather
            # than honouring it — and say so, because a forged id is a signal.
            logger.warning(
                "Portal session named organisation %s with no active membership for user %s",
                candidate,
                user.pk,
            )

        # No candidate, or an unusable one: fall back to the sole membership.
        # With more than one and no valid choice, make the user choose rather
        # than picking for them.
        memberships = list(user.active_memberships()[:2])
        if len(memberships) == 1:
            request.session[PORTAL_ORG_SESSION_KEY] = memberships[0].organization_id
            return memberships[0]
        return None

    def _refuse(self, request, user):
        """Rule 3. Flush and refuse — never continue unbound into a view."""
        logger.error(
            "Portal request from user %s with no usable membership; session flushed", user.pk
        )
        request.session.flush()
        return JsonResponse(
            {
                "detail": "No active organisation for this account.",
                "code": "no_active_membership",
            },
            status=401,
        )
