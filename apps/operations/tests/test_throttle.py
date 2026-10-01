"""Per-address throttling, and getting the address right.

The per-email bucket alone never sees an attacker spraying one guess each
across many accounts. Arch §431 lets this plane be identity-aware INSTEAD of
IP-restricted, and with TOTP live that is the option taken — so nothing at the
network layer is covering that case.

The half worth testing hardest is `client_ip`. Reading `X-Forwarded-For` and
taking the FIRST entry is the classic implementation, and it is worse than no
throttle at all: the header is caller-supplied, so one attacker presenting a
different forged value per request is never counted, while the logs look fine.
"""
from __future__ import annotations

import pytest
from django.test import RequestFactory
from django.urls import reverse

from apps.operations.models import OperatorUser
from apps.operations.throttle import Bucket, client_ip

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def _isolated_cache(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ops-throttle-tests",
        }
    }
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def request_from(peer="203.0.113.9", forwarded=None):
    factory = RequestFactory()
    extra = {"REMOTE_ADDR": peer}
    if forwarded is not None:
        extra["HTTP_X_FORWARDED_FOR"] = forwarded
    return factory.post("/login", **extra)


# ── Which address ───────────────────────────────────────────────────────────


def test_without_a_trusted_proxy_the_header_is_ignored(settings):
    """Unset depth means a direct connection, so the peer is the only honest
    answer — and believing the header there would be free spoofing."""
    settings.TRUSTED_PROXY_DEPTH = 0

    assert client_ip(request_from(peer="198.51.100.7", forwarded="1.2.3.4")) == "198.51.100.7"


def test_with_one_proxy_the_last_entry_wins(settings):
    """Caddy APPENDS the real peer, so the client is on the right and anything
    to its left arrived from the caller."""
    settings.TRUSTED_PROXY_DEPTH = 1

    request = request_from(peer="172.18.0.2", forwarded="203.0.113.9")
    assert client_ip(request) == "203.0.113.9"


def test_a_forged_header_cannot_move_the_bucket(settings):
    """The attack this exists to stop: a different forged value per request
    would mean unlimited attempts, counted against nobody."""
    settings.TRUSTED_PROXY_DEPTH = 1

    forged = request_from(peer="172.18.0.2", forwarded="9.9.9.9, 203.0.113.9")
    assert client_ip(forged) == "203.0.113.9", "the forged entry is to the LEFT"

    # Every forged prefix still resolves to the same real client.
    addresses = {
        client_ip(request_from(peer="172.18.0.2", forwarded=f"{n}.{n}.{n}.{n}, 203.0.113.9"))
        for n in range(1, 6)
    }
    assert addresses == {"203.0.113.9"}


def test_two_proxies_count_two_from_the_right(settings):
    settings.TRUSTED_PROXY_DEPTH = 2

    request = request_from(peer="172.18.0.2", forwarded="9.9.9.9, 203.0.113.9, 10.0.0.1")
    assert client_ip(request) == "203.0.113.9"


def test_fewer_hops_than_expected_falls_back_to_the_peer(settings):
    """The request did not come through the proxies this is configured for."""
    settings.TRUSTED_PROXY_DEPTH = 2

    assert client_ip(request_from(peer="198.51.100.7", forwarded="1.2.3.4")) == "198.51.100.7"


def test_a_missing_header_falls_back_to_the_peer(settings):
    settings.TRUSTED_PROXY_DEPTH = 1

    assert client_ip(request_from(peer="198.51.100.7")) == "198.51.100.7"


def test_an_unconfigured_proxy_is_warned_about(settings, caplog):
    """Either a proxy is in front and nobody set the depth, or someone is
    probing. Both are worth saying once."""
    settings.TRUSTED_PROXY_DEPTH = 0

    with caplog.at_level("WARNING"):
        client_ip(request_from(forwarded="1.2.3.4"))

    assert any("TRUSTED_PROXY_DEPTH" in r.getMessage() for r in caplog.records)


# ── The bucket ──────────────────────────────────────────────────────────────


def test_a_bucket_fills_and_stops():
    bucket = Bucket("test", limit=3, window=60)

    for _ in range(3):
        assert not bucket.exceeded
        bucket.record()

    assert bucket.exceeded


def test_clearing_resets_it():
    bucket = Bucket("test", limit=1, window=60)
    bucket.record()
    assert bucket.exceeded

    bucket.clear()
    assert not bucket.exceeded


def test_a_zero_limit_means_no_ceiling():
    """So a deployment can turn one off without the code branching on it."""
    bucket = Bucket("test", limit=0, window=60)
    for _ in range(50):
        bucket.record()

    assert not bucket.exceeded


def test_counting_is_atomic():
    """Two simultaneous attempts reading-then-writing would count once."""
    bucket = Bucket("test", limit=10, window=60)

    totals = [bucket.record() for _ in range(5)]

    assert totals == [1, 2, 3, 4, 5]


# ── Through the login view ──────────────────────────────────────────────────


@pytest.fixture
def _ops_plane(settings):
    assert settings.ROOT_URLCONF == "config.urls_ops"
    settings.OPERATOR_REQUIRE_MFA = True
    settings.TRUSTED_PROXY_DEPTH = 1


def test_spraying_across_accounts_is_bounded_by_the_address(client, settings, _ops_plane):
    """The hole the per-email bucket leaves open: one guess each against many
    accounts never locks any single account."""
    settings.OPERATOR_LOGIN_MAX_ATTEMPTS = 5
    settings.OPERATOR_LOGIN_MAX_ATTEMPTS_PER_IP = 3

    for n in range(3):
        response = client.post(
            reverse("ops-login"),
            {"email": f"victim{n}@pureplay.example", "password": "guess"},
            HTTP_X_FORWARDED_FOR="203.0.113.9",
        )
        assert response.status_code == 401, "no single account has locked"

    blocked = client.post(
        reverse("ops-login"),
        {"email": "victim99@pureplay.example", "password": "guess"},
        HTTP_X_FORWARDED_FOR="203.0.113.9",
    )
    assert blocked.status_code == 429


def test_one_blocked_address_does_not_block_another(client, settings, _ops_plane):
    """An office behind one NAT must not take everyone else down with it."""
    settings.OPERATOR_LOGIN_MAX_ATTEMPTS_PER_IP = 1
    OperatorUser.objects.create_user(
        email="real@pureplay.example", password=PASSWORD, name="Real"
    )

    client.post(
        reverse("ops-login"),
        {"email": "a@pureplay.example", "password": "guess"},
        HTTP_X_FORWARDED_FOR="203.0.113.9",
    )
    assert client.post(
        reverse("ops-login"),
        {"email": "b@pureplay.example", "password": "guess"},
        HTTP_X_FORWARDED_FOR="203.0.113.9",
    ).status_code == 429

    # A different address is unaffected, and a correct password still works.
    elsewhere = client.post(
        reverse("ops-login"),
        {"email": "real@pureplay.example", "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="198.51.100.7",
    )
    assert elsewhere.status_code == 302


def test_a_successful_sign_in_clears_the_account_bucket_only(
    client, settings, _ops_plane
):
    """The address ceiling must SURVIVE a success.

    Otherwise an attacker who happens to hold one valid account resets the
    spraying counter at will, and the per-address limit means nothing.
    """
    from django.core.cache import cache

    settings.OPERATOR_LOGIN_MAX_ATTEMPTS_PER_IP = 10
    OperatorUser.objects.create_user(
        email="real@pureplay.example", password=PASSWORD, name="Real"
    )

    for n in range(2):
        client.post(
            reverse("ops-login"),
            {"email": f"victim{n}@pureplay.example", "password": "guess"},
            HTTP_X_FORWARDED_FOR="203.0.113.9",
        )
    assert cache.get("ops-login-ip:203.0.113.9") == 2

    signed_in = client.post(
        reverse("ops-login"),
        {"email": "real@pureplay.example", "password": PASSWORD},
        HTTP_X_FORWARDED_FOR="203.0.113.9",
    )

    assert signed_in.status_code == 302, "a correct password still works"
    assert cache.get("ops-login-ip:203.0.113.9") == 2, "the address count survived"
    assert cache.get("ops-login:real@pureplay.example") is None, "the account cleared"
