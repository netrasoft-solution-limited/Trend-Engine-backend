"""URL patterns for the tenant plane.

Mounted by config/urls_portal.py, which contains ONE include. Everything the
client can reach is here, and this module reaches content only through
`apps.publication` — never `apps.outputs` (Arch §9.3).
"""
from django.urls import path

from . import views
from . import views_content

urlpatterns = [
    # ── Authentication ──────────────────────────────────────────────────────
    path("api/auth/csrf", views.CsrfView.as_view(), name="portal-csrf"),
    path("api/auth/login", views.LoginView.as_view(), name="portal-login"),
    path("api/auth/logout", views.LogoutView.as_view(), name="portal-logout"),
    path("api/auth/session", views.SessionView.as_view(), name="portal-session"),
    path("api/auth/org", views.SwitchOrgView.as_view(), name="portal-switch-org"),
    path(
        "api/auth/password-reset",
        views.PasswordResetRequestView.as_view(),
        name="portal-password-reset",
    ),
    path(
        "api/auth/password-reset/confirm",
        views.PasswordResetConfirmView.as_view(),
        name="portal-password-reset-confirm",
    ),
    # Self-service registration — 404 unless PORTAL_ALLOW_SELF_SIGNUP.
    path("api/auth/register", views.RegisterView.as_view(), name="portal-register"),
    path("api/auth/verify", views.VerifyEmailView.as_view(), name="portal-verify-email"),
    path(
        "api/auth/verify/resend",
        views.ResendVerificationView.as_view(),
        name="portal-verify-resend",
    ),
    path("api/invites/accept", views.InviteAcceptView.as_view(), name="portal-invite-accept"),

    # ── Team (Org Admin only, enforced in the view) ─────────────────────────
    path("api/team", views.TeamView.as_view(), name="portal-team"),
    path("api/team/<int:membership_id>", views.TeamMemberView.as_view(), name="portal-team-member"),

    # ── Content, all of it through the publication gate ─────────────────────
    path("api/publications", views_content.PublicationListView.as_view(), name="portal-publications"),
    path(
        "api/publications/<int:publication_id>",
        views_content.PublicationDetailView.as_view(),
        name="portal-publication",
    ),
    path("api/deliveries", views_content.DeliveryListView.as_view(), name="portal-deliveries"),
    path("api/notifications", views_content.NotificationListView.as_view(), name="portal-notifications"),
    path(
        "api/notifications/preferences",
        views_content.NotificationPreferenceView.as_view(),
        name="portal-notification-prefs",
    ),
    path("api/subscription", views_content.SubscriptionView.as_view(), name="portal-subscription"),
]
