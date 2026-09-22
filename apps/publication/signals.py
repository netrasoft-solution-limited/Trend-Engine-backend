"""Events the gate emits.

`publication` sits BELOW `portal` in the layer stack (Arch §4), so it must not
import it — the `.importlinter` layers contract would reject
`from apps.portal... import ...` here, and rightly: the gate must not depend on
who happens to be listening.

Notifying the client is a portal concern triggered by a publication event, so
the gate emits and `apps.portal` connects. The receiver runs inside the
publish transaction, so an output cannot go live without its notification being
written.
"""
from django.dispatch import Signal

#: Sent after a Publication row exists and the audit event is written.
#: kwargs: publication
published = Signal()

#: Sent after a publication is withdrawn. kwargs: publication, reason
unpublished = Signal()
