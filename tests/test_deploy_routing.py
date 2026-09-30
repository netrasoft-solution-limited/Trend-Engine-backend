"""The two planes reach browsers on two origins, and the edge has to agree.

Every defect this file guards against is invisible in Python and invisible in
a unit test of a view. They show up only in a browser, usually as "nobody can
log in", and the previous Caddyfile had two of them at once:

  · Both planes on one origin. `__Host-` cookies require `Path=/` exactly
    (RFC 6265bis §4.1.3.2) and both settings modules set that, so a shared
    origin meant two `Path=/` cookies for one host, each sent to both planes.
  · Cookie `Path` is matched against the REQUEST url, not the calling page, so
    a stored XSS on a portal page could have called `/ops/…` with
    `credentials: 'include'` and had the operator cookie attached. Only a
    separate origin stops that.

Reading a config file in a test is unusual and deliberate: the deployment
topology is load-bearing for the security model, and nothing else checks it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
CADDYFILE = BACKEND / "deploy" / "Caddyfile"
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = BACKEND / "deploy" / ".env.example"


@pytest.fixture(scope="module")
def caddyfile() -> str:
    assert CADDYFILE.exists(), "the Caddyfile is the edge; it must exist"
    return CADDYFILE.read_text()


def _directives(text: str) -> str:
    """The file without comments, so a rule described in prose is not mistaken
    for a rule in force — most of this file is explanation."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


# ── The origin split ────────────────────────────────────────────────────────


def test_the_operator_plane_has_its_own_origin(caddyfile):
    assert re.search(r"^ops\.\{\$SITE_DOMAIN\}\s*\{", _directives(caddyfile), re.M), (
        "The operator plane must be served from ops.<domain>. On a shared "
        "origin the two __Host- session cookies collide and a portal XSS can "
        "call the operator plane with credentials attached."
    )


def test_the_planes_are_not_split_by_path(caddyfile):
    """A `/ops/*` matcher is the old, broken topology."""
    directives = _directives(caddyfile)

    assert "handle /ops/" not in directives and "handle_path /ops/" not in directives, (
        "Path-based plane routing is what this deployment moved away from. "
        "The operator plane owns the whole of ops.<domain>."
    )


def test_each_plane_proxies_to_its_own_process(caddyfile):
    """Two WSGI processes is the entire point of ADR #3. One upstream serving
    both origins would give the portal the operator URLconf."""
    directives = _directives(caddyfile)

    assert "web-ops:8000" in directives
    assert "web-portal:8000" in directives

    ops_block = directives.split("ops.{$SITE_DOMAIN}")[1].split("\n{$SITE_DOMAIN}")[0]
    assert "web-portal" not in ops_block, "the ops origin must not reach the portal process"


# ── Ordering, which is silent when wrong ────────────────────────────────────


def test_the_portal_api_is_matched_before_the_spa_fallback(caddyfile):
    """Otherwise every XHR is answered with index.html and a 200.

    That surfaces as a JSON parse error in the browser console, a long way
    from the cause, and the API looks like it is returning corrupt data.
    """
    directives = _directives(caddyfile)
    api = directives.find("handle /portal/api/")
    spa = directives.find("handle /portal/*")

    assert api != -1, "the portal API needs its own matcher"
    assert spa != -1, "the portal SPA needs a matcher"
    assert api < spa, "the API matcher must come before the SPA catch-all"


def test_the_apex_redirect_carries_an_explicit_matcher(caddyfile):
    """`redir /portal/ permanent` does not do what it reads like.

    Caddy's grammar is `redir [<matcher>] <to> [<code>]`, so it takes
    `/portal/` as a PATH MATCHER and `permanent` as the destination. The
    fallback then matches almost nothing and answers an empty 200 — the apex
    silently serves a blank page. It validates cleanly either way; only an
    actual request reveals it, which is how it survived in this file.
    """
    directives = _directives(caddyfile)

    assert "redir * /portal/" in directives, (
        "The apex fallback needs the explicit `*` matcher: "
        "`redir * /portal/ permanent`."
    )
    assert not re.search(r"redir\s+/portal/\s+permanent", directives), (
        "Without a matcher, `permanent` is parsed as the destination URL."
    )


def test_the_spa_falls_back_to_index_for_deep_links(caddyfile):
    """A password-reset link from an email is a deep link into the client
    router. Without try_files it 404s before React ever loads."""
    assert "try_files {path} /index.html" in caddyfile


# ── The things a deploy forgets ─────────────────────────────────────────────


def test_the_operator_plane_is_not_left_publicly_open_by_accident(caddyfile):
    """PRD §7.1: the internal tool is "private / identity-aware; not public".

    The IP allowlist is commented out on purpose — an empty allowlist locks
    out the person deploying — so this asserts the reminder survives, not that
    the restriction is live.
    """
    assert "OPS_ALLOWED_IPS" in caddyfile
    assert "Restrict this before launch" in caddyfile


def test_both_origins_are_documented_for_dns(caddyfile):
    """The `ops.` A record is the one that gets forgotten, and the symptom is
    a TLS failure on a hostname nobody has tried yet."""
    assert "DNS" in caddyfile


def test_the_environment_example_carries_both_planes_hosts():
    """Each process validates its OWN Host header and CSRF origin. Blank, the
    plane falls back to a shared list that will not contain the ops subdomain,
    and every POST 400s with a message that never names the setting.
    """
    env = ENV_EXAMPLE.read_text()

    for key in (
        "OPS_ALLOWED_HOSTS",
        "OPS_TRUSTED_ORIGINS",
        "PORTAL_ALLOWED_HOSTS",
        "PORTAL_TRUSTED_ORIGINS",
        "ACME_EMAIL",
    ):
        assert f"{key}=" in env, f"{key} is read by the settings but not documented"


def test_csrf_origins_carry_a_scheme_and_allowed_hosts_do_not():
    """Django's own asymmetry, and a common way to lose an afternoon."""
    env = ENV_EXAMPLE.read_text()

    for line in env.splitlines():
        if line.startswith(("OPS_TRUSTED_ORIGINS=", "PORTAL_TRUSTED_ORIGINS=")):
            assert "https://" in line, f"CSRF origins need a scheme: {line}"
        if line.startswith(("OPS_ALLOWED_HOSTS=", "PORTAL_ALLOWED_HOSTS=")):
            assert "//" not in line, f"ALLOWED_HOSTS must not carry a scheme: {line}"


# ── The bundle ──────────────────────────────────────────────────────────────


def test_something_actually_builds_and_serves_the_portal():
    """There was no such service for a long time: the stack ran a JSON API
    with no interface in front of it."""
    compose = COMPOSE.read_text()

    assert "Dockerfile.frontend" in compose, "nothing builds the portal bundle"
    assert "web:/srv/web" in compose, "Caddy is not given the built bundle"


def test_caddy_waits_for_the_bundle_to_be_written():
    """`service_started` would not do. Caddy serving an empty /srv/web answers
    every portal request with a 404 that reads like a routing bug."""
    compose = COMPOSE.read_text()

    assert "service_completed_successfully" in compose


# ── Consistency with the Django side ────────────────────────────────────────


def test_the_ops_urlconf_has_no_redundant_prefix():
    """On `ops.<domain>` a `/ops/` prefix is redundant, and a HALF-applied one
    is worse than none — the API carried it while LOGIN_URL did not, so the
    two disagreed about where the plane began."""
    urls = (BACKEND / "config" / "urls_ops.py").read_text()

    assert 'path("ops/' not in urls, (
        "The operator plane owns its origin's root; drop the ops/ prefix."
    )


def test_both_planes_set_a_root_cookie_path_and_different_names():
    """`__Host-` requires Path=/ exactly, and two planes sharing a cookie name
    would overwrite each other even on separate origins in a browser that
    ignores the prefix."""
    from config.settings import ops, portal

    assert ops.SESSION_COOKIE_PATH == "/"
    assert portal.SESSION_COOKIE_PATH == "/"
    assert ops.SESSION_COOKIE_NAME != portal.SESSION_COOKIE_NAME
