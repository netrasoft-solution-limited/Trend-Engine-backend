# Data only — no schema change.
#
# Registration no longer requires email verification. Anyone who registered
# before that change and never clicked their link is still
# PENDING_VERIFICATION, which `active_memberships()` excludes, so they could
# never log in. This does for each of them what `VerifyEmailView` used to:
#   · the membership becomes ACTIVE, `accepted_at` set;
#   · its organisation becomes ACTIVE, but only if it is still ONBOARDING —
#     one an operator has suspended in the meantime stays suspended;
#   · any still-live verification tokens are invalidated.
#
# Not reversible: afterwards there is no record of which memberships were
# pending, so the reverse is a no-op.

from django.db import migrations
from django.utils import timezone


def activate_pending_registrations(apps, schema_editor):
    OrgMembership = apps.get_model("portal", "OrgMembership")
    EmailVerificationToken = apps.get_model("portal", "EmailVerificationToken")
    Organization = apps.get_model("tenancy", "Organization")

    now = timezone.now()
    pending = OrgMembership.objects.filter(status="pending_verification")
    org_ids = list(pending.values_list("organization_id", flat=True))
    membership_ids = list(pending.values_list("pk", flat=True))

    Organization.objects.filter(pk__in=org_ids, status="onboarding").update(status="active")
    OrgMembership.objects.filter(pk__in=membership_ids).update(status="active", accepted_at=now)
    EmailVerificationToken.objects.filter(
        membership_id__in=membership_ids, used_at__isnull=True, invalidated_at__isnull=True
    ).update(invalidated_at=now)


class Migration(migrations.Migration):

    dependencies = [
        ("portal", "0002_self_service_registration"),
        ("tenancy", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(activate_pending_registrations, migrations.RunPython.noop),
    ]
