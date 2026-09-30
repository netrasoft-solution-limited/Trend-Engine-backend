"""Shared fixtures.

Deliberately at the repo root rather than under tests/, so that app-level test
packages get the same tenancy fixtures without importing across test trees.

`make_row` is the load-bearing one. Arch §5.4 calls the scope-leakage suite
deployment-blocking, and a leakage suite is only worth the name if it runs
against every tenant-scoped model — including the one someone adds next month.
So the model list is DISCOVERED from the registry rather than hand-maintained,
and `make_row` builds a valid instance of whatever it finds by introspection.
The alternative — a factory per model — is the version that silently stops
covering things, because nothing fails when a new model is left out of it.
"""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.db import models
from django.utils import timezone

from apps.tenancy.managers import TenantScopedModel

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch, request):
    """Refuse outbound HTTP from the test suite.

    Not paranoia — it already happened. A test patched `collection.collect`
    while the task under test had bound the name at import, so the patch missed
    and the suite made live Taddy calls. It passed nothing useful, took forty
    seconds, and on a metered vendor it would have spent money in CI on every
    push.

    Blocked at the TRANSPORT, so the many tests that drive an
    `httpx.MockTransport` are unaffected — that is a different class and never
    reaches this one. A test that genuinely wants the network marks itself
    `@pytest.mark.live`; nothing in the suite does today.
    """
    if request.node.get_closest_marker("live"):
        return

    import httpx

    def refuse(self, request_, *args, **kwargs):
        raise RuntimeError(
            f"A test tried to reach {request_.url.host} over the network. "
            f"Tests must drive an httpx.MockTransport instead — a live call "
            f"here is slow, flaky, and on a metered vendor it costs money on "
            f"every CI run. If this is deliberate, mark the test @pytest.mark.live."
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


# ── Tenants ─────────────────────────────────────────────────────────────────


@pytest.fixture
def organization(db):
    """The default tenant for single-tenant tests."""
    from apps.tenancy.models import Organization

    return Organization.objects.create(
        slug="jarrow", name="Jarrow Formulas", status=Organization.Status.ACTIVE
    )


@pytest.fixture
def org_a(db):
    from apps.tenancy.models import Organization

    return Organization.objects.create(
        slug="org-a", name="Org A", status=Organization.Status.ACTIVE, is_fixture=True
    )


@pytest.fixture
def org_b(db):
    """The second tenant. Isolation tests are vacuous without it.

    PRD §2 requires a second-client fixture precisely so that the leakage suite
    has something to fail against.
    """
    from apps.tenancy.models import Organization

    return Organization.objects.create(
        slug="org-b", name="Org B", status=Organization.Status.ACTIVE, is_fixture=True
    )


@pytest.fixture
def operator(db):
    from apps.operations.models import OperatorUser

    return OperatorUser.objects.create_user(
        email="operator@pureplay.test", password="not-a-real-password", name="Test Operator"
    )


# ── Building rows for models this file has never heard of ───────────────────


def tenant_scoped_models() -> list[type]:
    """Every concrete tenant-scoped model in the project.

    Discovered, not listed. A new model inheriting `TenantScopedModel` joins the
    leakage suite the moment it is written, which is the only version of this
    that stays true.
    """
    return sorted(
        (
            model
            for model in django_apps.get_models()
            if issubclass(model, TenantScopedModel) and not model._meta.abstract
        ),
        key=lambda m: m._meta.label,
    )


def _value_for(field, organization, seen: set):
    """A valid value for one field, chosen from its type and constraints."""
    if field.choices:
        return field.choices[0][0]

    if isinstance(field, models.ForeignKey):
        target = field.remote_field.model
        if target._meta.label == "tenancy.Organization":
            return organization
        return _build(target, organization, seen)

    if isinstance(field, models.SlugField):
        return f"slug-{next(_counter)}"
    if isinstance(field, models.EmailField):
        return f"user{next(_counter)}@example.test"
    if isinstance(field, models.URLField):
        return f"https://example.test/{next(_counter)}"
    if isinstance(field, (models.CharField, models.TextField)):
        return f"fixture-{next(_counter)}"
    if isinstance(field, models.DecimalField):
        return Decimal("1.00")
    if isinstance(field, models.FloatField):
        return 1.0
    if isinstance(field, models.IntegerField):
        # Unique-ish, because several of these models have a uniqueness
        # constraint on a number within a parent.
        return next(_counter)
    if isinstance(field, models.BooleanField):
        return False
    if isinstance(field, models.DateTimeField):
        return timezone.now()
    if isinstance(field, models.DateField):
        return timezone.now().date()
    if isinstance(field, models.JSONField):
        return {}
    return None


def _needs_a_value(field) -> bool:
    """Whether this field must be supplied for `create` to succeed."""
    if field.auto_created or not field.concrete:
        return False
    if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False):
        return False
    if field.has_default() or field.null or field.blank:
        return False
    return True


def _build(model: type, organization, seen: set):
    """Create one valid instance of `model`, recursing into required FKs."""
    label = model._meta.label
    if label in seen:
        # A self-referential required FK cannot be satisfied by construction.
        raise pytest.skip.Exception(f"{label} has a cyclic required foreign key")
    seen = seen | {label}

    values = {}
    for field in model._meta.get_fields():
        if not _needs_a_value(field):
            continue
        value = _value_for(field, organization, seen)
        if value is not None:
            values[field.name] = value

    # `unscoped` where it exists, rather than `objects`: this is fixture setup,
    # deliberately outside the tenant binding the test is about to establish.
    # Going through the scoped manager would mean binding a tenant just to
    # create the row that proves binding works. Required FKs often point at
    # GLOBAL models (a Signal, a ContentItem), which carry no `unscoped`.
    manager = getattr(model, "unscoped", None) or model._base_manager
    return manager.create(**values)


@pytest.fixture
def make_row(db):
    """Create a valid row of any tenant-scoped model, for a given tenant.

        make_row(Publication, organization=org_b)

    Called without a model, it uses the first registered tenant-scoped model —
    which is what the route tests want, since any scoped row will do.
    """

    def factory(model: type | None = None, *, organization, **overrides):
        model = model or tenant_scoped_models()[0]
        row = _build(model, organization, seen=set())
        if overrides:
            for key, value in overrides.items():
                setattr(row, key, value)
            row.save()
        return row

    return factory


# ── The portal plane ────────────────────────────────────────────────────────


@pytest.fixture
def portal_client():
    """Returns a Django test client authenticated as an OrgUser of the given org.

    Must build the client against `config.settings.portal`, not the operator
    settings — testing the portal through the operator URLconf would pass while
    proving nothing about the real request path.
    """
    from django.conf import settings

    if getattr(settings, "AUTH_USER_MODEL", "") != "portal.OrgUser":
        pytest.skip("Portal routes are only reachable under config.settings.portal")

    from django.test import Client

    from apps.portal.models import OrgMembership, OrgUser

    def factory(organization):
        user = OrgUser.objects.create_user(
            email=f"member{next(_counter)}@example.test",
            password="not-a-real-password",
            name="Test Member",
        )
        OrgMembership.objects.create(
            org_user=user,
            organization=organization,
            role=OrgMembership.Role.ORG_VIEWER,
            status=OrgMembership.Status.ACTIVE,
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["active_org_id"] = organization.pk
        session.save()
        return client

    return factory
