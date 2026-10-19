"""A transcript that is the same conversation as another is not extracted.

The duplicate rule itself is tested in `apps/evidence/tests/test_duplicates.py`;
these are the enrichment side of it, which the evidence layer may not import.
"""
from __future__ import annotations

import pytest

from apps.evidence.services import duplicates
from apps.evidence.tests import test_duplicates as shared

pytestmark = pytest.mark.django_db

EPISODE, item = shared.EPISODE, shared.item
sources = shared.sources


def test_a_duplicate_is_not_extracted(sources):
    from apps.enrichment import extraction

    podcast, youtube = sources
    item(podcast, "ep", text=EPISODE + " word3000")
    upload = item(youtube, "vid", text=EPISODE)
    duplicates.check(upload)
    upload.refresh_from_db()

    result = extraction.extract(upload, domain_pack="supplements")

    assert result.skipped == "same conversation as another item"


def test_the_transcript_task_stops_before_extraction_for_a_duplicate(sources, monkeypatch):
    from apps.enrichment import tasks
    from apps.evidence.services import transcripts

    podcast, youtube = sources
    item(podcast, "ep", text=EPISODE + " word3000")
    upload = item(youtube, "vid", text=EPISODE)
    monkeypatch.setattr(
        transcripts, "acquire",
        lambda i: transcripts.Acquisition(item_id=i.pk, method="taddy", chars=len(EPISODE)),
    )
    dispatched = []
    monkeypatch.setattr(tasks.extract_evidence, "delay", lambda pk: dispatched.append(pk))

    result = tasks.acquire_transcript(upload.pk)

    assert dispatched == []
    assert result["duplicate_of"] is not None
