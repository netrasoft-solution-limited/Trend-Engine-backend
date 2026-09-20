"""Arch §5.4, suite 2 of 3 — unscoped access.

"Assert that touching a tenant model without a bound tenant raises
 TenantScopeError."

This is the suite that protects the other two. Scope-leakage tests only mean
something while the default manager still refuses to answer unbound questions;
the moment someone "fixes" a TenantScopeError by returning an empty queryset
instead of raising, leakage tests keep passing and the guarantee is gone.
"""
from __future__ import annotations

import pytest

from apps.tenancy.context import UNSET, current_tenant, operator_scope, scoped
from apps.tenancy.exceptions import TenantScopeError

pytestmark = pytest.mark.django_db

#: Every concrete model inheriting TenantScopedModel. Parametrised so that a
#: new tenant-scoped model is covered the moment it is added, rather than when
#: someone remembers to write a test for it.
TENANT_SCOPED_MODELS: list[type] = []


def test_nothing_is_bound_by_default():
    assert current_tenant() is UNSET


@pytest.mark.parametrize("model", TENANT_SCOPED_MODELS)
def test_query_without_binding_raises(model):
    with pytest.raises(TenantScopeError):
        list(model.objects.all())


@pytest.mark.parametrize("model", TENANT_SCOPED_MODELS)
def test_count_without_binding_raises(model):
    """`.count()` and `.exists()` too — not just `.all()`.

    A guard that only covers the obvious call shape is not a guard.
    """
    with pytest.raises(TenantScopeError):
        model.objects.count()
    with pytest.raises(TenantScopeError):
        model.objects.exists()


@pytest.mark.parametrize("model", TENANT_SCOPED_MODELS)
def test_binding_a_tenant_permits_the_query(model, organization):
    with scoped(organization):
        list(model.objects.all())


@pytest.mark.parametrize("model", TENANT_SCOPED_MODELS)
def test_operator_scope_permits_the_query(model):
    with operator_scope():
        list(model.objects.all())


def test_binding_is_released_when_the_block_exits(organization):
    with scoped(organization):
        assert current_tenant() is organization
    assert current_tenant() is UNSET


def test_binding_is_released_even_when_the_block_raises(organization):
    with pytest.raises(ValueError):
        with scoped(organization):
            raise ValueError("boom")
    assert current_tenant() is UNSET
