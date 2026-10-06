"""Signal receivers. Connected in `PortalConfig.ready()`.

`apps.publication` sits below this app and must not import it, so the gate
emits and this module listens. Both receivers run inside the gate's
transaction, so a publication cannot exist without its notification and a
withdrawal cannot happen silently.

EMAIL IS NO LONGER SENT FROM HERE. It moved to `apps.publication.notifications`
when the client portal was mothballed: recipients now come from
`apps.clients.contacts` (L5), which the gate can read directly, so the notice no
longer has to be routed up to this layer through a signal. What remains here is
the in-app `PortalNotification` row.

That row is written for a plane that is not currently deployed. It is kept
rather than removed because the portal code is mothballed, not deleted — and
because `INSTALLED_APPS` is identical on both planes, so this receiver runs in
the operator process too, where writing a row costs nothing and deleting the
model would cost a migration against a plane we were told to keep.
"""
from __future__ import annotations

from django.dispatch import receiver

from apps.publication.signals import published, unpublished

from .models import PortalNotification


@receiver(published, dispatch_uid="portal.notify_published")
def on_published(sender, publication, **kwargs) -> None:
    PortalNotification.objects.create(
        organization=publication.organization,
        publication=publication,
        subject=f"New {publication.type_label.lower()}: {publication.title}",
        body=publication.summary,
    )


@receiver(unpublished, dispatch_uid="portal.notify_unpublished")
def on_unpublished(sender, publication, reason: str = "", **kwargs) -> None:
    """A withdrawal always records — PRD §6.9 makes publication reversible, and
    a client who was reading something must be told it was pulled. The telling
    is `publication.notifications.send_withdrawal_email`; this is the record."""
    PortalNotification.objects.create(
        organization=publication.organization,
        publication=None,
        subject="A published output has been withdrawn",
        body=(
            f"“{publication.title}” has been withdrawn while it is corrected. "
            f"A corrected version will follow."
        ),
    )
