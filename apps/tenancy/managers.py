"""Default-deny managers.

Arch §5.2: isolation lives in the persistence layer, NOT in views or
serializers — "those are too easy to forget".

The shape below follows the architecture's conceptual sketch, with one addition
it implies but does not spell out: `all_tenants()`, so that the rare legitimate
cross-tenant read is an explicit call a reviewer can find, rather than a
developer quietly swapping in `_base_manager` to make an error go away.
"""
from __future__ import annotations

from django.db import models

from .context import OPERATOR_ALL, UNSET, current_tenant
from .exceptions import TenantScopeError


class TenantScopedQuerySet(models.QuerySet):
    """QuerySet for models carrying `organization`."""

    def for_organization(self, organization) -> "TenantScopedQuerySet":
        return self.filter(organization=organization)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Default queryset raises unless a tenant is bound to the request context."""

    def get_queryset(self) -> TenantScopedQuerySet:
        tenant = current_tenant()

        if tenant is UNSET:
            raise TenantScopeError(
                f"{self.model.__name__} accessed without tenant scope. "
                f"Bind one with tenancy.context.scoped(org), or — if this is "
                f"genuinely operator-wide — with operator_scope()."
            )

        if tenant is OPERATOR_ALL:
            return super().get_queryset()

        return super().get_queryset().filter(organization_id=tenant.id)

    def all_tenants(self) -> TenantScopedQuerySet:
        """Every row, regardless of binding.

        For migrations, management commands and aggregate reporting. Never call
        this from a view. It is a named method so that `grep -rn all_tenants`
        returns the complete list of places that read across tenants.
        """
        return super().get_queryset()


class TenantScopedModel(models.Model):
    """Base for every tenant-scoped model (L5 and above).

    `objects` is default-deny. `unscoped` exists because Django needs an
    unfiltered manager for migrations, `dumpdata` and related-object descriptor
    traversal — but it is listed second, so `objects` remains `_default_manager`
    and anything that forgets to choose gets the safe one.
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = TenantScopedManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True
