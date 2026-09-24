"""Outbound email for the tenant plane.

PRD §13 lists the transactional email provider as an OPEN DECISION. Until it is
made, everything routes through Django's configured EMAIL_BACKEND, which is the
console backend in development. Choosing a provider should be a settings
change, not a code change — so nothing in this module knows a vendor name.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _portal_url(path: str) -> str:
    base = getattr(settings, "PORTAL_PUBLIC_URL", "http://localhost:5173")
    return f"{base.rstrip('/')}{path}"


def send_invite_email(invite, *, raw_token: str) -> None:
    """The one place the raw token leaves the system."""
    link = _portal_url(f"/portal/accept-invite/{raw_token}")
    send_mail(
        subject=f"You have been invited to the {invite.organization.name} portal",
        message=(
            f"{invite.organization.name} has invited you to read the category "
            f"intelligence Pure Play prepares for them.\n\n"
            f"Set your password and sign in:\n{link}\n\n"
            f"This link works once and expires on "
            f"{invite.expires_at:%d %B %Y}. If you were not expecting it, ignore it."
        ),
        from_email=None,
        recipient_list=[invite.email],
        fail_silently=False,
    )
    logger.info("Invite sent to %s for organisation %s", invite.email, invite.organization_id)


def send_verification_email(membership, *, raw_token: str) -> None:
    """The one place a raw verification token leaves the system."""
    link = _portal_url(f"/portal/verify-email/{raw_token}")
    send_mail(
        subject=f"Confirm your email to activate {membership.organization.name}",
        message=(
            f"Thanks for registering {membership.organization.name}.\n\n"
            f"Confirm this address to activate the organisation, then sign in:\n"
            f"{link}\n\n"
            f"This link works once and expires in 24 hours. If you did not "
            f"register, ignore this message and nothing will be activated."
        ),
        from_email=None,
        recipient_list=[membership.org_user.email],
        fail_silently=False,
    )
    logger.info(
        "Verification email sent for organisation %s", membership.organization_id
    )


def send_password_reset_email(user, *, uid: str, token: str) -> None:
    link = _portal_url(f"/portal/reset-password/{uid}/{token}")
    send_mail(
        subject="Reset your portal password",
        message=(
            f"Someone asked to reset the password for this address.\n\n"
            f"{link}\n\n"
            f"If that was not you, nothing has changed and you can ignore this."
        ),
        from_email=None,
        recipient_list=[user.email],
        fail_silently=False,
    )
