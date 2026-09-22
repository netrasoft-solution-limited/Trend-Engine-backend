"""Everything the client reads — `/portal/api/*`.

Two rules govern every view in this module.

1. CONTENT COMES THROUGH THE GATE. These views call
   `apps.publication.services` and never touch `apps.outputs`. That is not
   politeness; it is the `gate-is-the-only-door` contract in `.importlinter`,
   and it is what makes "approved is not published" true in code rather than
   only in the operator UI.

2. THE TENANT IS NOT A PARAMETER. Scoping comes from the organisation
   `PortalTenantMiddleware` bound after verifying membership. No view here
   accepts an organisation id, so no view here can be tricked into reading
   another tenant's rows.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Invoice, Subscription
from apps.publication import services as gate

from .models import NotificationPreference, PortalNotification
from .views import IsOrgAdmin

#: PRD §6.8. Publication and withdrawal notices cannot be switched off — a
#: withdrawal is precisely when silence would be worst.
PREFERENCE_CATALOGUE = [
    {"key": "published", "label": "When an output is published to us", "locked": True,
     "detail": "Sent to every member of the organisation."},
    {"key": "withdrawn", "label": "When a published output is withdrawn", "locked": True,
     "detail": "Cannot be turned off — withdrawals always notify."},
    {"key": "digest", "label": "Weekly summary of what was delivered", "locked": False,
     "detail": "One message on Monday morning."},
    {"key": "billing", "label": "Subscription and invoice notices", "locked": False,
     "detail": "Org Admins only.", "admin_only": True},
]


def _publication_card(publication) -> dict:
    """List shape. Deliberately excludes `body` — a list of twelve briefs
    should not ship twelve full documents."""
    return {
        "id": publication.pk,
        "type": publication.type,
        "type_label": publication.type_label,
        "title": publication.title,
        "summary": publication.summary,
        "published_at": publication.published_at,
    }


class PublicationListView(APIView):
    def get(self, request):
        rows = [_publication_card(p) for p in gate.published_for()]
        return Response(rows)


class PublicationDetailView(APIView):
    def get(self, request, publication_id: int):
        publication = gate.publication_by_id(publication_id)
        if publication is None:
            # 404 rather than 403, for both the withdrawn case and the
            # wrong-tenant case. A 403 would confirm the record exists, which
            # is itself a disclosure across a tenant boundary.
            return Response(
                {
                    "detail": "This isn't available. It may have been withdrawn while it is corrected.",
                    "code": "not_available",
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        card = _publication_card(publication)
        card["body"] = publication.body
        return Response(card)


class DeliveryListView(APIView):
    """The client-facing tracker.

    The collapse to "in preparation" happens in `apps.publication`, not here —
    it needs to read Output, which this app may not import. The portal receives
    only the collapsed value.
    """

    def get(self, request):
        return Response(gate.delivery_status_for())


class NotificationListView(APIView):
    def get(self, request):
        rows = [
            {
                "id": n.pk,
                "subject": n.subject,
                "body": n.body,
                "channel": n.channel,
                "sent_at": n.sent_at,
                "read": n.read_at is not None,
                "publication_id": n.publication_id,
            }
            for n in PortalNotification.objects.all()[:100]
        ]
        return Response(rows)

    def post(self, request):
        """Mark everything read. Idempotent."""
        PortalNotification.objects.filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationPreferenceView(APIView):
    def get(self, request):
        stored = {
            p.key: p.enabled
            for p in NotificationPreference.objects.filter(org_user=request.user)
        }
        rows = []
        for item in PREFERENCE_CATALOGUE:
            if item.get("admin_only") and request.org_role != "org_admin":
                continue
            rows.append(
                {
                    "key": item["key"],
                    "label": item["label"],
                    "detail": item["detail"],
                    "locked": item["locked"],
                    "enabled": True if item["locked"] else stored.get(item["key"], True),
                }
            )
        return Response(rows)

    def patch(self, request):
        key = request.data.get("key")
        enabled = bool(request.data.get("enabled"))

        item = next((i for i in PREFERENCE_CATALOGUE if i["key"] == key), None)
        if item is None:
            return Response({"detail": "Unknown preference."}, status=status.HTTP_400_BAD_REQUEST)
        if item["locked"]:
            return Response(
                {
                    "detail": "Publication and withdrawal notices cannot be switched off.",
                    "code": "locked_preference",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        NotificationPreference.objects.update_or_create(
            organization=request.organization,
            org_user=request.user,
            key=key,
            defaults={"enabled": enabled},
        )
        return Response({"key": key, "enabled": enabled})


class SubscriptionView(APIView):
    """Org Admin only — PRD §3.2 keeps billing away from an Org Viewer."""

    permission_classes = [IsOrgAdmin]

    def get(self, request):
        subscription = Subscription.objects.select_related("plan").first()
        payload = {
            "subscription": None,
            "invoices": [
                {
                    "id": inv.pk,
                    "number": inv.number,
                    "period": inv.period_label,
                    "amount": str(inv.amount),
                    "currency": inv.currency,
                    "status": inv.status,
                    "issued_on": inv.issued_on,
                    "method": inv.method,
                }
                for inv in Invoice.objects.all()
            ],
        }
        if subscription is not None:
            payload["subscription"] = {
                "plan": subscription.plan.name,
                "status": subscription.status,
                "amount_monthly": str(subscription.plan.amount_monthly),
                "currency": subscription.plan.currency,
                "current_period": f"{subscription.current_period_start:%d %b} – "
                                  f"{subscription.current_period_end:%d %b %Y}",
                "renews_on": subscription.renews_on,
                # PRD §7.5: a processor reference, never card or bank data.
                # There is no field in this database that could hold one.
                "processor_ref": subscription.processor_ref,
            }
        return Response(payload)
