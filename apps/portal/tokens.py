"""Invite token handling.

The raw token is the credential. It is shown exactly once — in the email — and
never stored. Only a hash goes in the database, so a dump of `portal_orginvite`
yields nothing usable.

SHA-256 rather than a password hasher on purpose: the token is 256 bits of
`secrets.token_urlsafe` entropy, so it is not brute-forceable and does not need
a slow KDF. A slow hash here would only make invite lookup slow.
"""
from __future__ import annotations

import hashlib
import hmac

from django.conf import settings


def hash_invite_token(raw: str) -> str:
    """Keyed with SECRET_KEY so a stolen database cannot be used to build a
    rainbow table of plausible tokens offline."""
    return hmac.new(
        settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()


def tokens_match(raw: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_invite_token(raw), stored_hash)
