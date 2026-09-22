"""API-wide behaviour for the tenant plane.

DRF answers an unauthenticated request with 403 when the authentication class
cannot issue a challenge, which SessionAuthentication cannot. For a JSON client
that is the wrong signal: 403 means "you are known and not allowed", which the
React portal would render as a permissions message rather than sending the user
to sign in. This handler restores the distinction.
"""
from __future__ import annotations

from rest_framework import exceptions
from rest_framework.views import exception_handler as drf_exception_handler


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, exceptions.NotAuthenticated):
        response.status_code = 401
        response.data = {"detail": "Sign in to continue.", "code": "not_authenticated"}
    elif isinstance(exc, exceptions.PermissionDenied):
        response.data.setdefault("code", "permission_denied")

    return response
