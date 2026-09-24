"""Serializers for the tenant plane.

Everything here is read by a client. The rule that governs this module is
PRD §3.2: an org user never sees "raw evidence, internal scores, vendor costs,
other orgs, pre-publication content". So these are explicit field lists, never
`fields = "__all__"` — a wildcard would leak the next field someone adds to a
model without anyone noticing.
"""
from __future__ import annotations

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import OrgInvite, OrgMembership, OrgRole, OrgUser


class OrganizationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    slug = serializers.SlugField()
    name = serializers.CharField()


class MembershipSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer(read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = OrgMembership
        fields = ["id", "organization", "role", "role_label"]


class SessionSerializer(serializers.Serializer):
    """What `GET auth/session` returns. The React portal's entire idea of who
    it is talking to.

    `role` is the role in the ACTIVE organisation, resolved server-side. The
    client cannot set it — the mock portal used to let a viewer pick their own
    role from a dropdown.
    """

    id = serializers.IntegerField()
    email = serializers.EmailField()
    name = serializers.CharField()
    role = serializers.CharField()
    role_label = serializers.CharField()
    organization = OrganizationSerializer()
    memberships = MembershipSerializer(many=True)


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, style={"input_type": "password"})


class SwitchOrgSerializer(serializers.Serializer):
    organization_id = serializers.IntegerField()


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class _PasswordField(serializers.CharField):
    """Runs Django's AUTH_PASSWORD_VALIDATORS and reports every failure at once.

    DRF would otherwise surface only the first, which makes "your password is
    too short" and "your password is too common" a two-round-trip conversation.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("trim_whitespace", False)
        kwargs.setdefault("write_only", True)
        kwargs.setdefault("style", {"input_type": "password"})
        super().__init__(**kwargs)

    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return value


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = _PasswordField()


class InviteAcceptSerializer(serializers.Serializer):
    token = serializers.CharField()
    name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    password = _PasswordField()


class RegisterSerializer(serializers.Serializer):
    """Self-service registration of a new organisation.

    There is no `role` field, deliberately: the registrant is always the
    organisation's admin, and a role sent by the client is ignored rather than
    validated.
    """

    organization_name = serializers.CharField(max_length=200)
    name = serializers.CharField(max_length=200)
    #: OrgUser.email is varchar(254). EmailValidator alone admits up to 320
    #: characters, and 255–320 would reach the INSERT and fail there as a 500.
    email = serializers.EmailField(max_length=254)
    password = _PasswordField()


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=128)


class ResendVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()


class InviteCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=OrgRole.choices, default=OrgRole.VIEWER)


class TeamMemberSerializer(serializers.ModelSerializer):
    """One row on the Team screen.

    `last_login` comes from the user; everything else from the membership, so a
    consultant at two organisations shows the right role in each.
    """

    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(source="org_user.name", read_only=True)
    email = serializers.EmailField(source="org_user.email", read_only=True)
    last_login = serializers.DateTimeField(source="org_user.last_login", read_only=True)
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = OrgMembership
        fields = ["id", "name", "email", "role", "role_label", "status", "last_login"]


class PendingInviteSerializer(serializers.ModelSerializer):
    role_label = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model = OrgInvite
        fields = ["id", "email", "role", "role_label", "expires_at", "created_at"]


def session_payload(user: OrgUser, membership: OrgMembership) -> dict:
    """The one place the session shape is built, so login, session and
    org-switch cannot drift apart."""
    return {
        "id": user.pk,
        "email": user.email,
        "name": user.name,
        "role": membership.role,
        "role_label": membership.get_role_display(),
        "organization": {
            "id": membership.organization_id,
            "slug": membership.organization.slug,
            "name": membership.organization.name,
        },
        "memberships": MembershipSerializer(user.active_memberships(), many=True).data,
    }
