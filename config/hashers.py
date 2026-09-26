"""Password hashing parameters sized for the host, not Django's defaults.

Django's Argon2PasswordHasher uses 100 MiB per hash. On a 512 MB instance
running one gunicorn worker, that one allocation is enough to get the worker
SIGKILLed mid-request, which is what registration and login were doing. This
uses OWASP's minimum Argon2id profile instead: m=19 MiB, t=2, p=1.

An accepted, temporary trade — less resistance to offline cracking in exchange
for fitting the free tier. Revisit when the instance grows.

The algorithm name stays "argon2", so this REPLACES Django's hasher in
PASSWORD_HASHERS rather than sitting beside it. Every argon2 hash, old or new,
is checked by this class, and `must_update` flags any hash made with other
parameters so Django rehashes it at the next successful login. Verifying an
old hash still costs the 100 MiB it was made with, because Argon2 reads its
parameters from the stored hash.
"""
from __future__ import annotations

from django.contrib.auth.hashers import Argon2PasswordHasher


class LowMemoryArgon2PasswordHasher(Argon2PasswordHasher):
    time_cost = 2
    memory_cost = 19 * 1024  # KiB
    parallelism = 1
