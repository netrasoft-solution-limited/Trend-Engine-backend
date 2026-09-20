"""Shared fixtures.

Deliberately at the repo root rather than under tests/, so that app-level test
packages get the same tenancy fixtures without importing across test trees.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def organization(db):
    """The default tenant for single-tenant tests."""
    raise NotImplementedError("Create an Organization once apps.tenancy has migrations")


@pytest.fixture
def org_a(db):
    raise NotImplementedError


@pytest.fixture
def org_b(db):
    """The second tenant. Isolation tests are vacuous without it."""
    raise NotImplementedError


@pytest.fixture
def operator(db):
    raise NotImplementedError


@pytest.fixture
def portal_client():
    """Returns a Django test client authenticated as an OrgUser of the given org.

    Must build the client against `config.settings.portal`, not the operator
    settings — testing the portal through the operator URLconf would pass while
    proving nothing about the real request path.
    """
    raise NotImplementedError
