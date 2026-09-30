"""The operator second factor — PRD §7.1's "MFA where supported".

Until this existed, `OPERATOR_REQUIRE_MFA` made ops login refuse everyone in
production: the password was checked and then rejected with
`mfa_not_implemented`. That was the right fail-closed choice, and it meant the
entire operator plane — triage, the source registry, provider credentials — was
unreachable on a real deployment.

Three properties are worth more than the rest here.

THE SECRET IS ENCRYPTED AT REST. A TOTP secret in a database dump lets whoever
holds it generate valid codes forever, silently, and the account looks
perfectly normal while they do. It is stored through the same Fernet helper the
vendor credentials use.

A CODE CANNOT BE REPLAYED. A TOTP code stays valid for its whole 30-second
step, so one observed over someone's shoulder — or captured by anything sitting
in front of the login form — works again until that step ends. The counter of
the last accepted code is stored, and anything at or below it is refused.

LOSING A PHONE IS RECOVERABLE. Without recovery codes, one lost device locks a
person out of the operator plane permanently, and the only way back is SSH and
a management command. They are single-use and stored hashed, so the database
does not hold a second set of usable credentials.
"""
from __future__ import annotations

import logging
import secrets
import time

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password

from apps.sources import credentials

logger = logging.getLogger(__name__)

#: The RFC 6238 default, and what every authenticator app assumes.
STEP_SECONDS = 30

#: One step either side, so a phone clock a few seconds out still works. Wider
#: would extend the window in which an observed code is useful; narrower would
#: generate support requests from correct behaviour.
VALID_WINDOW = 1

RECOVERY_CODE_COUNT = 10


def new_secret() -> str:
    """A fresh base32 TOTP secret."""
    import pyotp

    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str) -> str:
    """The `otpauth://` URI an authenticator app scans.

    The issuer is what the person sees in their app, so it names the system
    rather than the domain — someone holding codes for several environments
    needs to tell them apart at a glance.
    """
    import pyotp

    issuer = getattr(settings, "OPERATOR_MFA_ISSUER", "Trend Engine")
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def current_counter(at: float | None = None) -> int:
    """Which 30-second step we are in. The replay guard's unit."""
    return int((at if at is not None else time.time()) // STEP_SECONDS)


def verify(secret: str, code: str, *, last_counter: int = 0) -> int | None:
    """Check a code, and return the counter it belongs to.

    Returns None when the code is wrong, expired, or ALREADY USED — a caller
    cannot tell those apart, and should not: distinguishing "wrong code" from
    "used code" tells an attacker their guess was right.

    The returned counter must be persisted by the caller, or the replay guard
    does nothing.
    """
    import pyotp

    if not secret or not code:
        return None

    cleaned = code.strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit():
        return None

    totp = pyotp.TOTP(secret)
    now = time.time()

    # Check each step in the window explicitly, rather than pyotp's `verify`,
    # because the counter of the code that matched is what the replay guard
    # needs and `verify` only answers yes or no.
    for offset in range(-VALID_WINDOW, VALID_WINDOW + 1):
        at = now + (offset * STEP_SECONDS)
        counter = current_counter(at)
        if counter <= last_counter:
            # Already used, or older than one we have accepted. Not an error —
            # just not acceptable again.
            continue
        if secrets.compare_digest(totp.at(at), cleaned):
            return counter

    return None


# ── Storage ─────────────────────────────────────────────────────────────────


def encrypt_secret(secret: str) -> str:
    return credentials.encrypt({"totp": secret})


def decrypt_secret(ciphertext: str) -> str:
    """The stored secret, or "" when nothing is stored.

    An unreadable secret — a changed encryption key — propagates as
    `CredentialUnreadable` rather than being swallowed, because silently
    treating it as "no MFA configured" would quietly weaken every account at
    once.
    """
    if not ciphertext:
        return ""
    return credentials.decrypt(ciphertext).get("totp", "")


# ── Recovery codes ──────────────────────────────────────────────────────────


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Human-transcribable single-use codes.

    Crockford-ish alphabet: no I, L, O, U, so a code read off a screen and
    typed on a phone does not fail on a character nobody can distinguish.
    """
    alphabet = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalise_recovery_code(code: str) -> str:
    return code.strip().upper().replace(" ", "").replace("-", "")


def hash_recovery_code(code: str) -> str:
    """Hashed with the password hasher — Argon2, per PASSWORD_HASHERS.

    A recovery code IS a credential; storing one reversibly would mean the
    database held a second usable way into every operator account.
    """
    return make_password(normalise_recovery_code(code))


def check_recovery_code(code: str, hashed: str) -> bool:
    return check_password(normalise_recovery_code(code), hashed)
