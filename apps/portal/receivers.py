"""Signal receivers. Connected in `PortalConfig.ready()`.

`apps.publication` sits below this app and must not import it, so the gate
emits and this module listens. Both receivers run inside the gate's
transaction, so a publication cannot exist without its notification and a
withdrawal cannot happen silently.
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
    """A withdrawal always notifies — PRD §6.9 makes publication reversible,
    and a client who was reading something must be told it was pulled."""
    PortalNotification.objects.create(
        organization=publication.organization,
        publication=None,
        subject="A published output has been withdrawn",
        body=(
            f"“{publication.title}” has been withdrawn while it is corrected. "
            f"A corrected version will follow."
        ),
    )
