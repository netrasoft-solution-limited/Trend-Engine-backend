"""The pipeline's joins.

The tasks themselves are thin — the gate, the ladder and the extractor are
tested elsewhere. What is worth asserting here is what each task does NEXT,
because a wrong hand-off is silent: items simply stop moving and the funnel
looks like a quiet week.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.enrichment import tasks
from apps.evidence.models import ContentItem, TranscriptArtifact
from apps.evidence.services import transcripts
from apps.evidence.services.transcripts import Acquisition, RungResult, Status
from apps.sources.models import Source

pytestmark = pytest.mark.django_db


@pytest.fixture
def item():
    source = Source.objects.create(name="A podcast", route=Source.Route.PODCAST)
    return ContentItem.objects.create(
        source=source,
        external_id="ep-1",
        title="Magnesium and sleep",
        url="https://example.test/ep1.mp3",
        content_hash=ContentItem.hash_content("ep-1"),
        gate_decision=ContentItem.GateDecision.RELEVANT,
    )


@pytest.fixture
def dispatched(monkeypatch):
    """Capture `.delay(...)` instead of putting anything on a queue."""
    seen: list[tuple[str, tuple]] = []

    for name in ("assess_relevance", "acquire_transcript", "extract_evidence"):
        task = getattr(tasks, name)
        monkeypatch.setattr(
            task, "delay", lambda *a, _n=name: seen.append((_n, a)), raising=False
        )
    return seen


def outcome(**kwargs) -> Acquisition:
    base = dict(item_id=1, rungs=[RungResult("taddy", Status.SUCCEEDED)])
    base.update(kwargs)
    return Acquisition(**base)


# ── The hand-off after a transcript ─────────────────────────────────────────


def test_a_transcript_triggers_extraction(item, dispatched, monkeypatch):
    monkeypatch.setattr(
        transcripts,
        "acquire",
        lambda i: outcome(method=TranscriptArtifact.Method.TADDY, chars=500),
    )

    result = tasks.acquire_transcript(item.pk)

    assert ("extract_evidence", (item.pk,)) in dispatched
    assert result["method"] == TranscriptArtifact.Method.TADDY
    assert result["chars"] == 500


def test_no_transcript_means_no_extraction(item, dispatched, monkeypatch):
    """Extraction on a metadata-only item would be spending to be told no —
    `extraction.extract` refuses it anyway, so dispatching is pure waste."""
    monkeypatch.setattr(
        transcripts,
        "acquire",
        lambda i: outcome(rungs=[RungResult("taddy", Status.UNAVAILABLE)]),
    )

    tasks.acquire_transcript(item.pk)

    assert dispatched == []


def test_the_rungs_walked_are_reported_for_the_run_log(item, dispatched, monkeypatch):
    """Arch §7.2 wants the route attributable per item, and "Taddy said no,
    AssemblyAI produced it" answers most later questions about cost."""
    monkeypatch.setattr(
        transcripts,
        "acquire",
        lambda i: outcome(
            method=TranscriptArtifact.Method.ASSEMBLYAI,
            cost_usd=Decimal("0.1125"),
            rungs=[
                RungResult("taddy", Status.UNAVAILABLE, "no transcript"),
                RungResult("assemblyai", Status.SUCCEEDED),
            ],
        ),
    )

    result = tasks.acquire_transcript(item.pk)

    assert result["rungs"] == [("taddy", "unavailable"), ("assemblyai", "succeeded")]
    assert result["cost_usd"] == "0.1125"


# ── Retrying, and not retrying ──────────────────────────────────────────────


def test_a_transient_failure_is_retried(item, dispatched, monkeypatch):
    """The item has NOT been written off, so the work is still worth doing."""
    monkeypatch.setattr(
        transcripts,
        "acquire",
        lambda i: outcome(rungs=[RungResult("assemblyai", Status.FAILED, "timed out")]),
    )

    retried: list[str] = []

    # Patched on the task INSTANCE: `bind=True` makes `self` the task, and a
    # Celery task is a proxy whose type carries no `retry` to replace.
    def fake_retry(exc=None, **kwargs):
        retried.append(str(exc))
        raise RuntimeError("retry called")

    monkeypatch.setattr(tasks.acquire_transcript, "retry", fake_retry, raising=False)

    with pytest.raises(RuntimeError, match="retry called"):
        tasks.acquire_transcript(item.pk)

    assert retried, "a broken rung must schedule a retry"


def test_an_item_nothing_can_transcribe_is_not_retried(item, dispatched, monkeypatch):
    """Retrying would ask the same three vendors the same question forever."""
    monkeypatch.setattr(
        transcripts,
        "acquire",
        lambda i: outcome(rungs=[RungResult("taddy", Status.UNAVAILABLE)]),
    )

    def must_not_retry(**kwargs):
        raise AssertionError("a permanently untranscribable item must not be retried")

    monkeypatch.setattr(tasks.acquire_transcript, "retry", must_not_retry, raising=False)

    result = tasks.acquire_transcript(item.pk)

    assert result["method"] is None


def test_a_missing_item_is_not_an_error(dispatched):
    """A deleted item between dispatch and execution is ordinary."""
    assert tasks.acquire_transcript(999_999) == {"skipped": "no such item"}


# ── The gate's hand-off into the ladder ─────────────────────────────────────


def test_only_items_that_earned_a_transcript_get_one(item, dispatched, monkeypatch):
    """Arch §6.2: the gate exists to avoid paying for transcripts nobody needs."""
    from apps.enrichment import gate

    rejected = gate.GateResult(
        decision=ContentItem.GateDecision.REJECTED,
        reason="off topic",
        confidence=95,
    )
    monkeypatch.setattr(gate, "assess", lambda i, domain_pack: rejected)
    # The real `assess` writes its decision to the item, and `needs_transcript`
    # reads it back from there rather than from the return value.
    item.gate_decision = ContentItem.GateDecision.REJECTED
    item.save(update_fields=["gate_decision"])

    tasks.assess_relevance(item.pk)

    assert not any(name == "acquire_transcript" for name, _ in dispatched)


def test_a_sampled_rejection_still_gets_a_transcript(item, dispatched, monkeypatch):
    """Arch §6.2's ~5% sample. Putting a rejected item through anyway is the
    only way a gap in the taxonomy ever surfaces, so it must not be optimised
    away as "it was rejected, why pay"."""
    from apps.enrichment import gate

    sampled = gate.GateResult(
        decision=ContentItem.GateDecision.SAMPLED,
        reason="rejected, pulled through by the sample",
        confidence=90,
        sampled=True,
    )
    monkeypatch.setattr(gate, "assess", lambda i, domain_pack: sampled)
    item.gate_decision = ContentItem.GateDecision.SAMPLED
    item.save(update_fields=["gate_decision"])

    tasks.assess_relevance(item.pk)

    assert ("acquire_transcript", (item.pk,)) in dispatched
