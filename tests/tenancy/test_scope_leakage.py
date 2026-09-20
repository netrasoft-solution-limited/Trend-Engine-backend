"""Arch §5.4, suite 1 of 3 — scope leakage.

"For every tenant-scoped model, assert that querying as Org A never returns
 Org B rows, across every exposed route."

Two halves, and both matter. The model half is cheap and catches a missing
`organization` filter. The route half is what PRD §7.4 actually asks for —
"a dedicated test suite, not an assumption that operator-layer isolation covers
it" — because a leak usually arrives through a view that fetched by primary key
without scoping, not through a manager that forgot to filter.
"""
from __future__ import annotations

import pytest

from apps.tenancy.context import scoped

pytestmark = pytest.mark.django_db

TENANT_SCOPED_MODELS: list[type] = []

#: Every URL name the portal exposes that takes an object id. Each one is a
#: place a client could try another tenant's primary key.
PORTAL_DETAIL_ROUTES: list[str] = []


@pytest.mark.parametrize("model", TENANT_SCOPED_MODELS)
def test_org_a_never_sees_org_b_rows(model, org_a, org_b, make_row):
    make_row(model, organization=org_b)

    with scoped(org_a):
        assert not model.objects.exists()

    with scoped(org_b):
        assert model.objects.exists()


@pytest.mark.parametrize("route", PORTAL_DETAIL_ROUTES)
def test_portal_route_refuses_another_tenants_id(route, portal_client, org_a, org_b, make_row):
    """Requesting Org B's object as an Org A user must 404, not 403.

    404 rather than 403: a 403 confirms the object exists, which is itself a
    disclosure across a tenant boundary.
    """
    row = make_row(organization=org_b)
    response = portal_client(org_a).get(f"/portal/{route}/{row.pk}/")
    assert response.status_code == 404


def test_the_fixture_tenant_is_actually_populated(org_b, make_row):
    """Guards against the whole suite passing because Org B has no rows.

    PRD §2 requires a second-client fixture for exactly this reason. A leakage
    suite that runs against an empty second tenant proves nothing.
    """
    make_row(organization=org_b)
    with scoped(org_b):
        assert TENANT_SCOPED_MODELS, "no tenant-scoped models registered — suite is vacuous"
