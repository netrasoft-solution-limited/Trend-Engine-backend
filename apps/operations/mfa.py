"""Enrolment and challenge, over the primitives in `totp.py`.

THE PENDING STATE is the part to read carefully. A password that is correct but
unconfirmed by a second factor must not produce a session — a partially
authenticated session is just an authenticated session with a comment on it.
So `login` stores a marker naming the user and the time, and nothing else. The
operator is still anonymous to Django, `LoginRequiredMiddleware` still refuses
every other view, and only the two MFA endpoints read the marker.

That also solves the bootstrap. Requiring MFA while nobody has enrolled is the
deadlock the system is in today: you cannot enrol without signing in and cannot
sign in without enrolling. The pending state is exactly enough authority to
enrol and no more.

Self-enrolment on first sign-in is a deliberate tradeoff and worth naming:
whoever knows the password binds the authenticator. That is the standard
bootstrap and it is audited, but it means a leaked password before first login
is a full compromise. `manage.py operator_mfa` pre-enrols an account where that
matters, and resets one whose owner has lost their phone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from . import totp
from .models import AuditEvent, OperatorUser, RecoveryCode

logger = logging.getLogger(__name__)

#: Session keys for the pending state. Namespaced so nothing else collides.
PENDING_USER = "_mfa_pending_user"
PENDING_SINCE = "_mfa_pending_since"

#: How long a password remains good for completing the challenge. Long enough
#: to fetch a phone, short enough that an unattended browser on a shared
#: machine is not an open door.
PENDING_SECONDS = 10 * 60


@dataclass(frozen=True)
class Enrolment:
    secret: str
    provisioning_uri: str


class MfaError(RuntimeError):
    """Something about the second factor is wrong, in a way a user can fix."""


# ── The pending state ───────────────────────────────────────────────────────


def begin_pending(session, user: OperatorUser) -> None:
    session[PENDING_USER] = user.pk
    session[PENDING_SINCE] = timezone.now().isoformat()


def pending_user(session) -> OperatorUser | None:
    """The account half-way through signing in, if the marker is still valid."""
    user_id = session.get(PENDING_USER)
    since = session.get(PENDING_SINCE)
    if not user_id or not since:
        return None

    try:
        started = timezone.datetime.fromisoformat(since)
    except (TypeError, ValueError):
        clear_pending(session)
        return None

    if (timezone.now() - started).total_seconds() > PENDING_SECONDS:
        clear_pending(session)
        return None

    user = OperatorUser.objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        clear_pending(session)
    return user


def clear_pending(session) -> None:
    session.pop(PENDING_USER, None)
    session.pop(PENDING_SINCE, None)


# ── Enrolment ───────────────────────────────────────────────────────────────


def begin_enrolment(user: OperatorUser) -> Enrolment:
    """A fresh secret, stored but NOT yet enabled.

    `mfa_enabled` stays False until a code proves the authenticator can read
    it. A secret nobody has demonstrated they hold is not a second factor, it
    is a lockout waiting to happen — and re-issuing here means an abandoned
    half-enrolment never strands anyone.
    """
    secret = totp.new_secret()
    user.totp_secret = totp.encrypt_secret(secret)
    user.mfa_enabled = False
    user.totp_confirmed_at = None
    user.totp_last_counter = 0
    user.save(
        update_fields=[
            "totp_secret", "mfa_enabled", "totp_confirmed_at", "totp_last_counter",
        ]
    )
    return Enrolment(
        secret=secret, provisioning_uri=totp.provisioning_uri(secret, email=user.email)
    )


@transaction.atomic
def confirm_enrolment(user: OperatorUser, code: str) -> list[str]:
    """Prove the authenticator works, then turn the factor on.

    Returns the recovery codes ONCE. They are stored hashed, so this is the
    only moment they can be shown — which is why the caller must put them in
    front of the operator rather than logging them anywhere.
    """
    secret = totp.decrypt_secret(user.totp_secret)
    if not secret:
        raise MfaError("There is no enrolment in progress for this account.")

    counter = totp.verify(secret, code, last_counter=user.totp_last_counter)
    if counter is None:
        raise MfaError("That code is not right. Check the clock on your phone.")

    user.mfa_enabled = True
    user.totp_confirmed_at = timezone.now()
    user.totp_last_counter = counter
    user.save(update_fields=["mfa_enabled", "totp_confirmed_at", "totp_last_counter"])

    codes = _issue_recovery_codes(user)
    _audit(user, "operator enrolled a second factor")
    return codes


def _issue_recovery_codes(user: OperatorUser) -> list[str]:
    """Replace any existing codes. Re-enrolling invalidates the old set, which
    is the behaviour someone re-enrolling after a compromise expects."""
    RecoveryCode.objects.filter(operator_id=user.pk).delete()
    codes = totp.new_recovery_codes()
    RecoveryCode.objects.bulk_create(
        [
            RecoveryCode(operator_id=user.pk, code_hash=totp.hash_recovery_code(c))
            for c in codes
        ]
    )
    return codes


# ── The challenge ───────────────────────────────────────────────────────────


def verify_code(user: OperatorUser, code: str) -> bool:
    """A TOTP code, or a recovery code. Either completes the challenge.

    Tried in that order because a TOTP code is the common case and a recovery
    code is six characters longer — checking recovery first would mean hashing
    on every ordinary sign-in.
    """
    secret = totp.decrypt_secret(user.totp_secret)
    if secret:
        counter = totp.verify(secret, code, last_counter=user.totp_last_counter)
        if counter is not None:
            # Persisted immediately: this is what stops the same code being
            # replayed for the rest of its 30-second step.
            user.totp_last_counter = counter
            user.save(update_fields=["totp_last_counter"])
            return True

    return _consume_recovery_code(user, code)


@transaction.atomic
def _consume_recovery_code(user: OperatorUser, code: str) -> bool:
    """Single use, enforced by locking the row before marking it."""
    if not totp.normalise_recovery_code(code):
        return False

    unused = RecoveryCode.objects.select_for_update().filter(
        operator_id=user.pk, used_at__isnull=True
    )
    for candidate in unused:
        if totp.check_recovery_code(code, candidate.code_hash):
            candidate.used_at = timezone.now()
            candidate.save(update_fields=["used_at"])

            remaining = RecoveryCode.objects.filter(
                operator_id=user.pk, used_at__isnull=True
            ).count()
            _audit(user, f"operator signed in with a recovery code ({remaining} left)")
            if remaining == 0:
                logger.warning(
                    "%s has used their last recovery code. A lost authenticator "
                    "now means a reset over SSH.",
                    user.email,
                )
            return True
    return False


# ── Reset ───────────────────────────────────────────────────────────────────


@transaction.atomic
def reset(user: OperatorUser, *, actor_label: str) -> None:
    """Remove the second factor entirely, so the account can re-enrol.

    For a lost authenticator with no recovery codes left. It is a real
    privilege escalation path — it turns the account back into password-only
    until the next sign-in — so it is audited with who did it and it is not
    reachable from the web at all: `manage.py operator_mfa --reset` only.
    """
    user.totp_secret = ""
    user.mfa_enabled = False
    user.totp_confirmed_at = None
    user.totp_last_counter = 0
    user.save(
        update_fields=[
            "totp_secret", "mfa_enabled", "totp_confirmed_at", "totp_last_counter",
        ]
    )
    RecoveryCode.objects.filter(operator_id=user.pk).delete()
    _audit(user, f"second factor reset by {actor_label}; account re-enrols on next sign-in")


def _audit(user: OperatorUser, message: str) -> None:
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_id=user.pk,
        actor_label=user.email,
        message=message,
        context={"mfa": True},
    )
