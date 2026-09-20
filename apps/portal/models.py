"""portal — L7, TENANT-SCOPED.

PRD §8 models: OrgUser, OrgRole, PortalNotification, PortalSession.

Every model here carries `organization` and uses TenantScopedManager
as its default manager. That is not optional: an unscoped default
manager on a tenant model is the leak Arch §5.2 exists to prevent.
"""

from django.db import models  # noqa: F401
