"""Arch §5.4, suite 3 of 3 — cross-plane escalation.

"Assert a valid portal session cannot reach any /ops/* route or set operator
 scope."

Arch §5.3 makes escalation structurally impossible rather than merely
disallowed. These tests check both halves of that claim: the structural one
(separate processes, cookies and URLconfs) and the runtime one (the portal
context refuses to bind OPERATOR_ALL).
"""
from __future__ import annotations

import pytest

from apps.tenancy.context import bind_operator_all, mark_portal_plane
from apps.tenancy.exceptions import TenantEscalationError

pytestmark = pytest.mark.django_db


def test_portal_plane_cannot_bind_operator_scope():
    mark_portal_plane()
    with pytest.raises(TenantEscalationError):
        bind_operator_all()


def test_operator_urlconf_is_not_reachable_from_the_portal_process(portal_client, org_a):
    """The portal URLconf contains one include, and it is not the operator's."""
    response = portal_client(org_a).get("/ops/")
    assert response.status_code == 404


def test_portal_session_cookie_is_scoped_to_its_own_path(settings_portal):
    """Distinct name AND path.

    The path is what stops a browser from ever sending a portal session to an
    operator URL, which is a stronger guarantee than a server-side check.
    """
    assert settings_portal.SESSION_COOKIE_NAME == "__Host-te_portal"
    assert settings_portal.SESSION_COOKIE_PATH == "/portal"


def test_the_two_planes_share_no_cookie_name(settings_ops, settings_portal):
    assert settings_ops.SESSION_COOKIE_NAME != settings_portal.SESSION_COOKIE_NAME
    assert settings_ops.CSRF_COOKIE_NAME != settings_portal.CSRF_COOKIE_NAME


def test_the_two_planes_use_different_user_models(settings_ops, settings_portal):
    assert settings_ops.AUTH_USER_MODEL != settings_portal.AUTH_USER_MODEL


def test_portal_has_no_self_signup(settings_portal):
    """PRD §6.8: invite-based provisioning only."""
    assert settings_portal.PORTAL_ALLOW_SELF_SIGNUP is False
