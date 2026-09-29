"""Source: the status field behind "blocking failures"."""
from __future__ import annotations

import pytest

from apps.sources.models import Source

pytestmark = pytest.mark.django_db


def test_source_routes_and_statuses():
    assert Source.Route.values == ["podcast", "youtube", "research", "social"]
    assert Source.Status.values == ["active", "degraded", "failed"]


def test_a_new_source_is_active_and_has_never_succeeded():
    source = Source.objects.create(name="Huberman Lab (RSS)", route=Source.Route.PODCAST)

    assert source.status == Source.Status.ACTIVE
    assert source.last_success_at is None
