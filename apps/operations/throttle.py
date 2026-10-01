"""Rate limiting for operator sign-in, per account AND per source address.

The per-email bucket on its own has a hole: five bad attempts against one
address locks that address, but an attacker spraying one guess each across many
addresses is never throttled. Arch §431 permits the operator plane to be
"identity-aware OR IP-restricted", and with TOTP live the first half is
satisfied — so there is no network allowlist standing in front of this, and the
spraying case is reachable from the internet.

GETTING THE CLIENT ADDRESS RIGHT IS THE WHOLE PROBLEM. Behind a proxy,
`REMOTE_ADDR` is the proxy, so every request shares one bucket. The usual fix —
read `X-Forwarded-For` and take the first entry — is worse than no throttle at
all: the header is attacker-supplied, so a different forged value per request
means unlimited attempts while the logs look fine.

Caddy APPENDS the real peer to whatever arrived, so with one trusted proxy the
client is the LAST entry and the forged ones sit to its left. That is why this
counts from the right, by how many proxies are actually trusted, and why the
count defaults to ZERO: unset, nothing believes the header at all.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def client_ip(request) -> str:
    """The caller's address, trusting exactly as many proxies as configured.

    `TRUSTED_PROXY_DEPTH` is the number of proxies between the internet and
    Django — 1 for this deployment's Caddy. Zero means the header is ignored
    entirely, which is correct when Django is reached directly and is the safe
    default for anything that forgets to set it.
    """
    depth = int(getattr(settings, "TRUSTED_PROXY_DEPTH", 0) or 0)
    remote = request.META.get("REMOTE_ADDR", "") or ""

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if depth <= 0:
        if forwarded:
            # Either a proxy is in front and nobody configured the depth, or
            # someone is probing. Both are worth saying out loud once.
            logger.warning(
                "X-Forwarded-For present but TRUSTED_PROXY_DEPTH is 0, so it is "
                "ignored and throttling keys on %s. Set the depth if a proxy is "
                "in front of this process.",
                remote,
            )
        return remote

    hops = [part.strip() for part in forwarded.split(",") if part.strip()]
    if len(hops) >= depth:
        # Counted from the RIGHT. Anything further left was supplied by the
        # caller and cannot be trusted.
        return hops[-depth]

    # Fewer hops than expected: the request did not come through the proxies
    # this is configured for, so the peer address is the only honest answer.
    return remote


class Bucket:
    """A counter with a ceiling, over the cache.

    Deliberately not a sliding window. A fixed window lets roughly twice the
    limit through across a boundary, and for a login throttle that is fine —
    the job is to make guessing impractical, not to be exact.
    """

    def __init__(self, key: str, *, limit: int, window: int) -> None:
        self.key = key
        self.limit = limit
        self.window = window

    @property
    def exceeded(self) -> bool:
        if self.limit <= 0:
            return False
        return cache.get(self.key, 0) >= self.limit

    def record(self) -> int:
        """Count one attempt. Returns the new total.

        `add` then `incr` rather than get-then-set: two simultaneous attempts
        would otherwise read the same value and each write it back plus one,
        counting once.
        """
        cache.add(self.key, 0, self.window)
        try:
            return cache.incr(self.key)
        except ValueError:
            # The key expired between `add` and `incr`. The window just reset,
            # so this attempt is the first of a new one.
            cache.set(self.key, 1, self.window)
            return 1

    def clear(self) -> None:
        cache.delete(self.key)


def login_buckets(request, email: str) -> tuple[Bucket, Bucket]:
    """The two ceilings a sign-in attempt is measured against.

    The per-address limit is much higher than the per-account one, because an
    office behind one NAT is many legitimate people sharing an address — it is
    sized to stop spraying, not to stop a team arriving at nine o'clock.
    """
    window = settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS
    return (
        Bucket(
            f"ops-login:{email}",
            limit=settings.OPERATOR_LOGIN_MAX_ATTEMPTS,
            window=window,
        ),
        Bucket(
            f"ops-login-ip:{client_ip(request)}",
            limit=settings.OPERATOR_LOGIN_MAX_ATTEMPTS_PER_IP,
            window=window,
        ),
    )


def mfa_buckets(request, user_id: int) -> tuple[Bucket, Bucket]:
    window = settings.OPERATOR_LOGIN_ATTEMPT_WINDOW_SECONDS
    return (
        Bucket(
            f"ops-mfa:{user_id}",
            limit=settings.OPERATOR_MFA_MAX_ATTEMPTS,
            window=window,
        ),
        Bucket(
            f"ops-mfa-ip:{client_ip(request)}",
            limit=settings.OPERATOR_MFA_MAX_ATTEMPTS_PER_IP,
            window=window,
        ),
    )
