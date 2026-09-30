"""Storing vendor keys where an operator can rotate them.

The properties worth testing are the ones a reviewer would want proven before
trusting a secret to this: that the plaintext is not in the row, that the
database beats the environment, that a changed encryption key fails loudly
rather than falling back, and that the hint reveals nothing.
"""
from __future__ import annotations

import pytest

from apps.sources import credentials
from apps.sources.models import AcquisitionProvider

pytestmark = pytest.mark.django_db

#: Shaped like an Apify token, and deliberately not one. A real key in a
#: test fixture is a key in the repository, in every clone of it, and in
#: the history after it is "removed".
SECRET = "apify_api_EXAMPLEONLYnotarealkey0000000000"


@pytest.fixture
def provider():
    return AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.APIFY, name="Apify"
    )


def test_the_plaintext_is_not_in_the_stored_row(provider):
    """The whole point. A database dump must not yield the key."""
    provider.set_credentials({"api_key": SECRET}, actor_label="op@test")
    provider.save()

    provider.refresh_from_db()
    assert SECRET not in provider.credential_ciphertext
    assert SECRET not in provider.credential_hint
    assert provider.credential("api_key") == SECRET


def test_the_hint_shows_which_key_without_showing_the_key(provider):
    provider.set_credentials({"api_key": SECRET}, actor_label="op@test")

    assert provider.credential_hint == f"api_key ····{SECRET[-4:]}"
    assert len(provider.credential_hint) < 20


def test_a_provider_can_hold_more_than_one_secret(provider):
    """Taddy authenticates with a key AND a user id, and with neither alone."""
    taddy = AcquisitionProvider.objects.create(
        kind=AcquisitionProvider.Kind.TADDY, name="Taddy"
    )
    taddy.set_credentials({"api_key": "k-123456", "user_id": "9876"}, actor_label="op@test")
    taddy.save()

    assert taddy.credential("api_key") == "k-123456"
    assert taddy.credential("user_id") == "9876"


def test_the_database_beats_the_environment(provider, monkeypatch):
    """Rotation is the reason this field exists.

    An environment variable that silently won would make the operator screen a
    lie: the key would show as changed while the old one kept being used.
    """
    monkeypatch.setenv("APIFY_TOKEN", "the-old-key-from-the-env")
    provider.set_credentials({"api_key": SECRET}, actor_label="op@test")

    assert provider.credential("api_key") == SECRET


def test_the_environment_is_the_fallback_before_anything_is_stored(provider, monkeypatch):
    """A fresh checkout, CI and the first deploy all work before anyone has
    opened the operator screen."""
    monkeypatch.setenv("APIFY_TOKEN", "from-the-environment")

    assert not provider.has_credential
    assert provider.credential("api_key") == "from-the-environment"


def test_the_conventional_environment_variable_is_the_existing_one(provider):
    """Apify's key has always been APIFY_TOKEN, not APIFY_API_KEY. Renaming it
    would be a silent breakage on the next deploy for no gain."""
    assert provider.default_env_var("api_key") == "APIFY_TOKEN"

    assembly = AcquisitionProvider(kind=AcquisitionProvider.Kind.ASSEMBLYAI)
    assert assembly.default_env_var("api_key") == "ASSEMBLYAI_API_KEY"

    taddy = AcquisitionProvider(kind=AcquisitionProvider.Kind.TADDY)
    assert taddy.default_env_var("user_id") == "TADDY_USER_ID"


def test_replacing_credentials_does_not_merge(provider):
    """A partial update is how a stale second value survives a rotation and
    authentication keeps failing for a reason nobody can see."""
    provider.set_credentials({"api_key": "first", "user_id": "9876"}, actor_label="op@test")
    provider.set_credentials({"api_key": "second"}, actor_label="op@test")

    assert provider.credential("api_key") == "second"
    assert provider.credential("user_id") == ""


def test_clearing_removes_the_ciphertext(provider):
    provider.set_credentials({"api_key": SECRET}, actor_label="op@test")
    provider.set_credentials({}, actor_label="op@test")

    assert provider.credential_ciphertext == ""
    assert not provider.has_credential


def test_a_changed_encryption_key_fails_loudly(provider, settings):
    """Silence here would be the dangerous outcome.

    An unreadable credential that returned empty would fall through to the
    environment variable and appear to work, hiding the fact that every stored
    secret is now dead until someone noticed a different provider failing.
    """
    provider.set_credentials({"api_key": SECRET}, actor_label="op@test")
    settings.CREDENTIALS_ENCRYPTION_KEY = credentials.generate_key()

    with pytest.raises(credentials.CredentialUnreadable):
        provider.credential("api_key")


def test_an_explicit_key_is_used_over_the_derived_one(settings):
    settings.CREDENTIALS_ENCRYPTION_KEY = credentials.generate_key()
    ciphertext = credentials.encrypt({"api_key": SECRET})

    assert credentials.decrypt(ciphertext) == {"api_key": SECRET}


def test_encryption_is_not_deterministic():
    """Fernet includes a timestamp and IV, so the same secret stored twice does
    not produce the same ciphertext — no equality oracle across providers."""
    assert credentials.encrypt({"api_key": SECRET}) != credentials.encrypt({"api_key": SECRET})


def test_empty_values_are_not_stored():
    assert credentials.decrypt(credentials.encrypt({"api_key": "", "user_id": "x"})) == {
        "user_id": "x"
    }
