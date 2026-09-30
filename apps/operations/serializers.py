"""Request validation and response shapes for the operator auth API."""
from __future__ import annotations

from rest_framework import serializers

from .models import OperatorUser


class MfaCodeSerializer(serializers.Serializer):
    """A TOTP code or a recovery code.

    Not constrained to six digits: a recovery code is eleven characters with a
    hyphen, and both arrive at the same endpoint so the operator does not have
    to know which kind they are holding.
    """

    code = serializers.CharField(max_length=32, trim_whitespace=True)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, style={"input_type": "password"})


def session_payload(user: OperatorUser) -> dict:
    """The one place the operator session shape is built, so login and session
    cannot drift apart."""
    return {
        "id": user.pk,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "role_label": user.get_role_display(),
        "mfa_enabled": user.mfa_enabled,
    }
