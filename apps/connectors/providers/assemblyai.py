"""AssemblyAI — the last rung of the transcript fallback chain (Arch §7.2).

This is the expensive one, and it is last for that reason. The chain is:
publisher transcript → Taddy → creator captions → ASR. Everything above is
free or nearly so; this bills per hour of audio, so a connector that reaches
for it first would multiply the transcript budget by a large number without
improving a single output.

    universal-2       $0.15/hour   the default: accurate enough for claim
                                   extraction, and the cheapest current model
    universal-3-5-pro $0.21/hour   for audio the cheaper model struggles with

A 45-minute podcast costs about $0.11 at the default. That is more than the
gate and extraction for the same episode combined, which is the whole argument
for the relevance gate running BEFORE transcription rather than after.

WORD TIMINGS ARE THE POINT. AssemblyAI returns every word with a millisecond
offset, so unlike a bare transcript this produces the same `(seconds, char)`
anchors the SRT path produces — and a claim extracted from ASR can still say
where in the audio it was said.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from apps.connectors.base import (
    BaseConnector,
    Capability,
    ContentResult,
    CostEstimate,
    DateWindow,
    FetchPlan,
    ItemRef,
)

logger = logging.getLogger(__name__)

API = "https://api.assemblyai.com/v2"

#: USD per hour of audio, by model. Verified against AssemblyAI's published
#: pricing on 2026-09-30. Kept here rather than in settings because a wrong
#: number means a cap that does not fire; it belongs next to the code that
#: spends, where a reviewer will see it.
RATES = {
    "universal-2": Decimal("0.15"),
    "universal-3-5-pro": Decimal("0.21"),
}

DEFAULT_MODEL = "universal-2"


class AssemblyAIError(RuntimeError):
    """A failed transcription.

    `blocking` separates the two classes Arch §6.3 treats differently: a bad
    key or an exhausted balance stops the connector and alerts an operator;
    a timeout or a 5xx backs off and retries.
    """

    def __init__(self, message: str, *, blocking: bool = False) -> None:
        super().__init__(message)
        self.blocking = blocking


@dataclass(frozen=True)
class Transcription:
    """One completed transcript, with everything the evidence layer needs."""

    text: str
    #: `(start_seconds, char_index)` per word — the same shape `srt.to_text`
    #: returns, so `TranscriptArtifact.anchors` is populated identically
    #: whichever rung produced the transcript.
    anchors: list[tuple[float, int]]
    #: 0-100. AssemblyAI's own mean word confidence, not a guess: a noisy
    #: recording scores low and the claims drawn from it can be weighed
    #: accordingly.
    quality: int
    duration_seconds: float
    cost_usd: Decimal
    model: str


class AssemblyAIConnector(BaseConnector):
    """Transcribes audio that no cheaper rung could supply.

    Deliberately narrow: it does not discover and it does not fetch metadata,
    because AssemblyAI is not a source of content — it is a way of reading
    content another connector already found. `capabilities` says so, and
    `discover` refuses rather than returning nothing, since a connector that
    quietly discovers zero items looks identical to a source that has gone
    quiet.
    """

    capabilities = {Capability.TRANSCRIPT}

    def __init__(
        self,
        *,
        source,
        policy,
        api_key: str,
        model: str = DEFAULT_MODEL,
        transport: Any | None = None,
    ) -> None:
        super().__init__(source=source, policy=policy)
        if not api_key:
            raise ValueError(
                "AssemblyAI needs an API key. Set it on the AcquisitionProvider "
                "in the operator admin, or as ASSEMBLYAI_API_KEY."
            )
        if model not in RATES:
            raise ValueError(
                f"Unknown AssemblyAI model {model!r}. Known: {', '.join(sorted(RATES))}. "
                f"Refusing rather than transcribing at an unknown price."
            )
        self._api_key = api_key
        self.model = model
        self._transport = transport

    # ── Cost ────────────────────────────────────────────────────────────────

    def estimate_cost(self, plan: FetchPlan) -> CostEstimate:
        """Priced from the audio duration the caller supplies.

        `FetchPlan.refs` carries no duration, so this prices a conservative
        default hour per item. Callers that know the real length should use
        `price_for` — an estimate that under-reads is a cap that does not fire.
        """
        hours = Decimal(len(plan.refs) or 1)
        return CostEstimate(
            currency="USD",
            amount=RATES[self.model] * hours,
            units={"hours": float(hours), "model": self.model},
        )

    def price_for(self, duration_seconds: float) -> Decimal:
        hours = Decimal(str(duration_seconds)) / Decimal(3600)
        return (RATES[self.model] * hours).quantize(Decimal("0.000001"))

    # ── The one thing it does ───────────────────────────────────────────────

    def transcribe(
        self,
        audio_url: str,
        *,
        max_cost_usd: Decimal | None = None,
        expected_duration_seconds: float | None = None,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 1800.0,
    ) -> Transcription:
        """Submit audio, wait for the transcript, return it priced.

        `max_cost_usd` is checked BEFORE submission where a duration is known
        (Arch §10.2). Once the job is submitted the money is committed, so a
        cap consulted afterwards is a report, not a control.
        """
        if max_cost_usd is not None and expected_duration_seconds is not None:
            expected = self.price_for(expected_duration_seconds)
            if expected > max_cost_usd:
                raise AssemblyAIError(
                    f"Refusing to transcribe: {expected_duration_seconds / 60:.0f} "
                    f"minutes at {self.model} costs about ${expected}, over the "
                    f"${max_cost_usd} cap for this item.",
                    blocking=True,
                )

        transcript_id = self._submit(audio_url)
        payload = self._await_completion(
            transcript_id, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds
        )
        return self._to_transcription(payload)

    def fetch_content(self, ref: ItemRef) -> ContentResult:
        if not ref.url:
            return ContentResult(
                text=None, route="metadata_only", is_metadata_only=True, raw_checksum=""
            )
        result = self.transcribe(ref.url)
        return ContentResult(
            text=result.text,
            route="assemblyai",
            is_metadata_only=False,
            raw_checksum="",
        )

    def discover(self, window: DateWindow) -> Iterable[ItemRef]:
        raise NotImplementedError(
            "AssemblyAI transcribes audio another connector discovered; it is "
            "not itself a source of content."
        )

    def fetch_metadata(self, refs: list[ItemRef]) -> Iterable[dict]:
        raise NotImplementedError("AssemblyAI returns transcripts, not metadata.")

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(
            timeout=timeout,
            transport=self._transport,
            headers={"Authorization": self._api_key},
        )

    def _submit(self, audio_url: str) -> str:
        body = {
            "audio_url": audio_url,
            # `speech_models`, plural and a list — the singular `speech_model`
            # is deprecated and now rejected outright. A list because the API
            # takes an ordered preference; we send exactly one, since a silent
            # fallback to a different model would mean billing at a rate this
            # connector did not price.
            "speech_models": [self.model],
            "language_detection": True,
            # Everything below is a separately-billed add-on that duplicates
            # work the enrichment layer does with its own cost accounting.
            "auto_chapters": False,
            "summarization": False,
            "iab_categories": False,
        }
        with self._client(60.0) as http:
            response = http.post(f"{API}/transcript", json=body)
        self._raise_for_status(response, "submit")
        return response.json()["id"]

    def _await_completion(
        self, transcript_id: str, *, poll_seconds: float, timeout_seconds: float
    ) -> dict:
        """Poll until the job finishes.

        Polling rather than a webhook because the caller is a Celery task on
        the `ingest` queue, which Arch §11.1 sizes for exactly this — "I/O
        bound, high concurrency, tolerant of slow external APIs". A webhook
        would need a public endpoint and a signature scheme to save a sleeping
        worker, which is the cheap resource here.
        """
        deadline = time.monotonic() + timeout_seconds

        while True:
            with self._client(30.0) as http:
                response = http.get(f"{API}/transcript/{transcript_id}")
            self._raise_for_status(response, "poll")
            payload = response.json()
            status = payload.get("status")

            if status == "completed":
                return payload
            if status == "error":
                raise AssemblyAIError(
                    f"AssemblyAI could not transcribe this audio: "
                    f"{payload.get('error', 'no reason given')}"
                )
            if time.monotonic() > deadline:
                raise AssemblyAIError(
                    f"AssemblyAI job {transcript_id} was still '{status}' after "
                    f"{timeout_seconds / 60:.0f} minutes. The job may still "
                    f"complete and will still be billed."
                )
            time.sleep(poll_seconds)

    def _raise_for_status(self, response: httpx.Response, stage: str) -> None:
        if response.status_code < 400:
            return
        raise AssemblyAIError(
            f"AssemblyAI {stage} returned {response.status_code}: {response.text[:300]}",
            blocking=response.status_code in {401, 402, 403},
        )

    # ── Mapping ─────────────────────────────────────────────────────────────

    def _to_transcription(self, payload: dict) -> Transcription:
        text = (payload.get("text") or "").strip()
        duration = float(payload.get("audio_duration") or 0)

        # `confidence` is a 0-1 mean over words. An empty transcript has no
        # confidence to report, and calling that 0 quality is right: there is
        # nothing to extract from it.
        confidence = payload.get("confidence")
        quality = int(round(float(confidence) * 100)) if confidence is not None else 0

        return Transcription(
            text=text,
            anchors=_anchors_from_words(payload.get("words") or [], text),
            quality=quality,
            duration_seconds=duration,
            cost_usd=self.price_for(duration),
            model=self.model,
        )


def _anchors_from_words(words: list[dict], text: str) -> list[tuple[float, int]]:
    """Turn per-word timings into `(seconds, char_index)` anchors.

    AssemblyAI gives a millisecond offset per word; what the evidence layer
    needs is a position in the transcript string. Rather than trusting that
    `" ".join(words)` reproduces `text` exactly — it does not, because
    punctuation and casing are applied separately — each word is LOCATED in
    the text, scanning forward. A word that cannot be found is skipped rather
    than guessed at: a wrong anchor is worse than a missing one, because it
    would put a claim at the wrong moment in the episode with full confidence.

    One anchor per second of audio at most. A 45-minute episode has ~7,000
    words, and an anchor per word would store more timing data than transcript
    for a resolution nobody reads.
    """
    anchors: list[tuple[float, int]] = []
    cursor = 0
    last_seconds = -1.0

    for word in words:
        token = (word.get("text") or "").strip()
        start_ms = word.get("start")
        if not token or start_ms is None:
            continue

        seconds = float(start_ms) / 1000.0
        if seconds - last_seconds < 1.0 and anchors:
            continue

        position = text.find(token, cursor)
        if position == -1:
            # Look ahead a little in case of a normalisation difference; give
            # up rather than anchor to the wrong occurrence.
            position = text.find(token, cursor, cursor + 200)
            if position == -1:
                continue

        anchors.append((seconds, position))
        cursor = position + len(token)
        last_seconds = seconds

    return anchors
