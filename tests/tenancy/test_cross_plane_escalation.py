"""Arch §5.4, suite 3 of 3 — cross-plane escalation.

"Assert a valid portal session cannot reach any /ops/* route or set operator
 scope."

Arch §5.3 makes escalation structurally impossible rather than merely
disallowed. These tests check both halves of that claim: the structural one
(separate origins, cookies, user models and URLconfs) and the runtime one (the
portal context refuses to bind OPERATOR_ALL).

The two settings modules are imported directly rather than through a fixture.
One process can only hold one app registry, so a test cannot *run under* both
planes — but it can compare their configuration, which is what the assertions
below are about.
"""
from __future__ import annotations

import importlib
import os

import pytest

from django.conf import settings

from apps.tenancy.context import (
    OPERATOR_ALL,
    bind_operator_all,
    current_tenant,
    is_portal_plane,
    portal_plane,
    reset,
)
from apps.tenancy.exceptions import TenantEscalationError

pytestmark = pytest.mark.tenancy

ops_settings = importlib.import_module("config.settings.ops")
portal_settings = importlib.import_module("config.settings.portal")


# ── The runtime guarantee ───────────────────────────────────────────────────

def test_portal_plane_cannot_bind_operator_scope():
    with portal_plane():
        with pytest.raises(TenantEscalationError):
            bind_operator_all()


def test_the_portal_flag_is_released_when_the_block_exits():
    """The flag used to be set and never reset.

    That made the guarantee leak across requests on a threaded worker, and made
    this suite order-dependent: one test marking the plane would poison every
    later test that legitimately expects operator scope.

    Asserted against the baseline rather than against a literal, because the
    baseline differs by plane: on the portal process `is_portal_plane()` is
    permanently True from settings, and no contextvar can lower it.
    """
    baseline = is_portal_plane()

    with portal_plane():
        assert is_portal_plane() is True

    assert is_portal_plane() is baseline


@pytest.mark.skipif(
    settings.TENANT_PLANE == "portal",
    reason="Operator scope is unreachable on the portal plane by design — that is the point.",
)
def test_operator_scope_still_binds_after_a_portal_block():
    """The companion to the test above, and the one that would have caught the
    leak: if the flag were not released, this would raise."""
    with portal_plane():
        pass

    token = bind_operator_all()
    try:
        assert current_tenant() is OPERATOR_ALL
    finally:
        reset(token)


def test_operator_scope_is_unreachable_on_the_portal_plane():
    """On the portal process the refusal holds with no contextvar involved at
    all — which is what makes it true for Celery tasks and management commands
    that never touch the middleware."""
    if settings.TENANT_PLANE != "portal":
        pytest.skip("Only meaningful in the portal process")

    with pytest.raises(TenantEscalationError):
        bind_operator_all()


def test_the_plane_is_a_property_of_the_process():
    """`TENANT_PLANE` is what makes the refusal hold for Celery tasks and
    management commands, not only for requests that reached the middleware."""
    assert ops_settings.TENANT_PLANE == "operator"
    assert portal_settings.TENANT_PLANE == "portal"


# ── The structural guarantee ────────────────────────────────────────────────

def test_the_two_planes_share_no_session_cookie():
    assert ops_settings.SESSION_COOKIE_NAME != portal_settings.SESSION_COOKIE_NAME


def test_session_cookies_are_host_prefixed_when_secure():
    """Arch §15.1 A2.

    §5.3 originally paired `__Host-` with `Path=/ops` and `Path=/portal`. No
    browser accepts that — the prefix requires `Path=/` exactly (RFC 6265bis
    §4.1.3.2), so the cookie was silently dropped and nobody could have logged
    into either plane. The planes are separated by ORIGIN instead, which lets
    both keep a genuine prefix.

    Local development cannot set a Secure cookie over plain HTTP, so the prefix
    is dropped there and only there.
    """
    for settings in (ops_settings, portal_settings):
        assert settings.SESSION_COOKIE_PATH == "/"
        if settings.SESSION_COOKIE_SECURE:
            assert settings.SESSION_COOKIE_NAME.startswith("__Host-")


def test_the_two_planes_use_different_user_models():
    assert ops_settings.AUTH_USER_MODEL == "operations.OperatorUser"
    assert portal_settings.AUTH_USER_MODEL == "portal.OrgUser"


def test_the_two_planes_use_different_auth_backends():
    """Django's `get_user()` refuses a session whose `_auth_user_backend` is not
    registered in the active process. Listing exactly one backend per plane is
    what makes an operator-authenticated cookie degrade to AnonymousUser if it
    is ever presented to the portal."""
    assert ops_settings.AUTHENTICATION_BACKENDS == ["apps.operations.auth.OperatorBackend"]
    assert portal_settings.AUTHENTICATION_BACKENDS == ["apps.portal.auth.PortalBackend"]


def test_the_portal_urlconf_does_not_include_the_operator_one():
    assert ops_settings.ROOT_URLCONF != portal_settings.ROOT_URLCONF

    portal_urls = importlib.import_module(portal_settings.ROOT_URLCONF)
    rendered = str([getattr(p, "urlconf_name", p) for p in portal_urls.urlpatterns])
    assert "urls_ops" not in rendered


# ── PRD §4.2 ────────────────────────────────────────────────────────────────

def test_self_service_signup_is_off_unless_deliberately_enabled(monkeypatch):
    """PRD §4.2 lists "self-serve client signup" as out of scope at launch, and
    §6.8 specifies invite-based provisioning only.

    The flag is env-driven so it can be exercised in development and in the
    smoke script. What must hold is that it is OFF when nobody has turned it
    on — an unset variable, or a fresh production environment, must not open
    registration. This reloads the module with the variable removed rather than
    asserting a literal, because the literal now depends on the environment.
    """
    monkeypatch.delenv("PORTAL_ALLOW_SELF_SIGNUP", raising=False)
    fresh = importlib.reload(importlib.import_module("config.settings.portal"))
    try:
        assert fresh.PORTAL_ALLOW_SELF_SIGNUP is False
    finally:
        # Restore the module to whatever this environment actually configures,
        # so the reload does not leak into later tests.
        os.environ.setdefault("PORTAL_ALLOW_SELF_SIGNUP", "0")
        importlib.reload(fresh)
