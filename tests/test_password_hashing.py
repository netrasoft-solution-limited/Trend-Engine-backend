"""The low-memory Argon2 hasher: new hashes use it, old hashes still verify
and are flagged for a rehash. No database needed."""
from __future__ import annotations

from django.contrib.auth.hashers import (
    Argon2PasswordHasher,
    check_password,
    identify_hasher,
    make_password,
)

from config.hashers import LowMemoryArgon2PasswordHasher


def test_new_hashes_use_the_low_memory_parameters():
    encoded = make_password("correct horse battery staple")
    assert isinstance(identify_hasher(encoded), LowMemoryArgon2PasswordHasher)
    assert "$m=19456,t=2,p=1$" in encoded


def test_a_hash_made_with_djangos_defaults_still_verifies_and_is_flagged():
    old = Argon2PasswordHasher().encode("correct horse battery staple", "saltsaltsaltsalt")
    assert "$m=102400," in old
    assert check_password("correct horse battery staple", old)
    assert not check_password("wrong", old)
    assert identify_hasher(old).must_update(old)


def test_a_new_hash_is_not_flagged():
    encoded = make_password("correct horse battery staple")
    assert not identify_hasher(encoded).must_update(encoded)
