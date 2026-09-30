"""Vendor credentials, encrypted at rest and editable by an operator.

WHY THIS EXISTS. Keys in the environment can only be changed by someone with
deploy access, which means a leaked Apify token waits for an engineer. Moving
them into the database makes rotation an operator action, and Arch §12 wants
provider configuration to be operator-managed rather than baked into a release.

WHAT THIS ACTUALLY PROTECTS AGAINST — worth being precise, because "encrypted"
invites more confidence than it earns. This protects a database dump: a stolen
backup, a mis-scoped read replica, a `pg_dump` in someone's downloads. It does
NOT protect against an attacker who already has application-level access, since
the running process must be able to decrypt to use the key at all. The honest
summary is that it turns N vendor secrets in the environment into ONE
encryption key, and makes vendor rotation a form submission instead of a deploy.

A provider may need more than one value — Taddy authenticates with an API key
AND a user id — so the stored blob is a small JSON object of named secrets
rather than a single string.
"""
from __future__ import annotations

import base64
import json
import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

#: Salt for the derived-key path. Constant on purpose: it is a domain separator,
#: not a secret, and changing it would orphan every stored credential.
_DERIVATION_SALT = b"trend-engine/provider-credentials/v1"


class CredentialError(RuntimeError):
    """A credential could not be read."""


class CredentialUnreadable(CredentialError):
    """Stored ciphertext did not decrypt.

    Almost always a changed encryption key. Raised rather than returning empty,
    because an empty credential would fall through to the environment variable
    and appear to work — hiding the fact that every stored key is now
    unreadable until someone notices a different provider failing.
    """


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImproperlyConfigured(
            "Provider credentials need the `cryptography` package. "
            "Run `pip install -e .` in backend/."
        ) from exc

    explicit = getattr(settings, "CREDENTIALS_ENCRYPTION_KEY", "")
    if explicit:
        return Fernet(explicit.encode() if isinstance(explicit, str) else explicit)

    # Derived from SECRET_KEY so that development and CI work with no extra
    # configuration. THE CONSEQUENCE IS REAL: rotating SECRET_KEY makes every
    # stored credential unreadable and they must be re-entered. Production sets
    # CREDENTIALS_ENCRYPTION_KEY explicitly so the two rotate independently.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    secret = getattr(settings, "SECRET_KEY", "")
    if not secret:
        raise ImproperlyConfigured("SECRET_KEY is required to derive a credential key.")

    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_DERIVATION_SALT, info=b"fernet"
    ).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(derived))


def generate_key() -> str:
    """A fresh CREDENTIALS_ENCRYPTION_KEY, for `manage.py` and the deploy docs."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def encrypt(values: dict[str, str]) -> str:
    """Encrypt a mapping of named secrets to storable text."""
    payload = json.dumps({k: v for k, v in values.items() if v}, sort_keys=True)
    return _fernet().encrypt(payload.encode()).decode()


def decrypt(ciphertext: str) -> dict[str, str]:
    if not ciphertext:
        return {}
    try:
        return json.loads(_fernet().decrypt(ciphertext.encode()).decode())
    except ImproperlyConfigured:
        raise
    except Exception as exc:
        raise CredentialUnreadable(
            "Stored provider credentials did not decrypt. This normally means "
            "CREDENTIALS_ENCRYPTION_KEY (or SECRET_KEY, if no explicit key is "
            "set) has changed since they were saved. They must be re-entered."
        ) from exc


def hint(values: dict[str, str]) -> str:
    """A human-checkable summary that reveals nothing useful.

    `api_key ····3f2a, user_id ····8e01` — enough for an operator to confirm
    which key is loaded without the interface ever displaying one. Nothing here
    may be reversible: that is the entire requirement.
    """
    parts = []
    for name in sorted(values):
        value = values[name] or ""
        parts.append(f"{name} ····{value[-4:]}" if len(value) >= 4 else f"{name} set")
    return ", ".join(parts)
