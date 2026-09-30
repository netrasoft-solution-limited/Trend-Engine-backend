"""The only thing in this system that talks to a model.

Three rules, enforced here so no call site can forget them:

  1. THE CAP IS CHECKED BEFORE THE CALL (Arch §10.2). A refused call costs
     nothing; a reconciled one has already been paid for.
  2. EVERY CALL WRITES A `ModelRun` AND A `CostEvent` — including the ones that
     fail. Arch §8.2 needs the model and prompt version to explain an output
     later; Arch §10.3 needs the tokens to replace the budget reserve with a
     measurement. A call that errors still cost money and still says something
     about reliability.
  3. THE MODEL IS CHOSEN BY TASK, NOT BY HABIT (Arch §10.3). "Tiered models by
     task. Cheap tier for relevance classification (high volume, low stakes);
     stronger tier only for client-facing drafting."

Prompt caching is not an optimisation here either. The domain pack taxonomy is
identical across every item in a run, so it goes in a cached system prefix and
is read back at a tenth of the price — which, at ~65 items a month against a
taxonomy of any size, is most of the gate's bill.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from django.conf import settings
from pydantic import BaseModel

from apps.enrichment.models import ModelRun

from . import costs, pricing

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """No credentials, or the SDK is not installed.

    Distinct from a refused call: this means the system cannot ask, not that it
    chose not to. Callers leave the item unassessed rather than rejecting it.
    """


class LLMInvalidOutput(RuntimeError):
    """The model answered, but not in the shape the schema requires."""


@dataclass(frozen=True)
class Tier:
    """A task's model and its ceiling.

    `max_output_tokens` is small for the gate on purpose: a classifier that
    wants to write an essay is misconfigured, and the ceiling is also what the
    pre-call estimate prices.
    """

    model: str
    max_output_tokens: int


def _tier(name: str, default_model: str, default_max: int) -> Tier:
    """Settings override the defaults, so a tier can be re-pointed without a
    deploy when a price or a capability changes."""
    configured = getattr(settings, f"LLM_MODEL_{name.upper()}", None)
    return Tier(model=configured or default_model, max_output_tokens=default_max)


def gate_tier() -> Tier:
    # Arch §6.2: "the cheapest viable model tier; this is a classification
    # task, not a reasoning task."
    return _tier("gate", "claude-haiku-4-5", 512)


def extraction_tier() -> Tier:
    # Structured, but it has to judge what is a claim and what is filler.
    return _tier("extraction", "claude-sonnet-5-5", 8000)


def drafting_tier() -> Tier:
    # Arch §10.3: "stronger tier only for client-facing drafting."
    return _tier("drafting", "claude-opus-5-5", 16000)


def _build_client() -> Any:
    """Construct the Anthropic client, or explain why we cannot.

    Imported lazily so the rest of the system — migrations, the portal, the
    tests that never call a model — runs without the SDK installed.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LLMUnavailable(
            "The `anthropic` package is not installed. `pip install -e .` in backend/."
        ) from exc

    api_key = getattr(settings, "LLM_API_KEY", "") or None
    try:
        # With no explicit key the SDK resolves ANTHROPIC_API_KEY, then
        # ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile — so a
        # developer who has logged in needs no LLM_API_KEY at all.
        return anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    except Exception as exc:  # pragma: no cover - construction rarely fails
        raise LLMUnavailable(f"Could not construct the Anthropic client: {exc}") from exc


class LLMClient:
    """Task-shaped methods over the Messages API.

    `transport` exists for tests: the suite drives a fake that returns canned
    structured output, so the gate and the extractor are testable without a
    key, without a network and without spending anything. Nothing else should
    pass it.
    """

    def __init__(self, transport: Any | None = None) -> None:
        self._transport = transport

    @property
    def client(self) -> Any:
        if self._transport is None:
            self._transport = _build_client()
        return self._transport

    # ── The one call path ───────────────────────────────────────────────────

    def structured(
        self,
        *,
        schema: type[T],
        tier: Tier,
        purpose: str,
        prompt_version: str,
        system: list[dict] | str,
        user: str,
        content_item: Any | None = None,
        estimated_input_tokens: int | None = None,
    ) -> T:
        """Ask for one schema-valid answer, and account for it.

        Uses `messages.parse`, which validates the response against the Pydantic
        model server-side and hands back a typed object. The alternative —
        asking for JSON in the prompt and hoping — is where extraction pipelines
        usually start leaking malformed rows into the database.
        """
        # Rough, and deliberately so: a token count that costs an API call to
        # obtain would make the guard more expensive than the thing it guards.
        # ~4 characters per token is close enough to refuse the right calls.
        approx_input = estimated_input_tokens or (len(str(system)) + len(user)) // 4
        estimate = pricing.estimate(
            tier.model, input_tokens=approx_input, max_output_tokens=tier.max_output_tokens
        )

        try:
            costs.check_or_raise(estimate, purpose=purpose)
        except costs.CostCapExceeded:
            self._record(
                purpose=purpose, outcome=ModelRun.Outcome.CAPPED, tier=tier,
                prompt_version=prompt_version, content_item=content_item,
                error=f"Refused before the call; estimated ${estimate:.4f}",
            )
            raise

        started = time.monotonic()
        try:
            response = self.client.messages.parse(
                model=tier.model,
                max_tokens=tier.max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except Exception as exc:
            self._record(
                purpose=purpose, outcome=ModelRun.Outcome.ERRORED, tier=tier,
                prompt_version=prompt_version, content_item=content_item,
                latency_ms=int((time.monotonic() - started) * 1000), error=str(exc)[:2000],
            )
            raise

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(response, "usage", None)
        tokens = {
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

        # A model may decline (`stop_reason: "refusal"`). That is a real
        # outcome, not an error — record it and let the caller decide, rather
        # than raising and losing the fact that it happened.
        if getattr(response, "stop_reason", None) == "refusal":
            self._record(
                purpose=purpose, outcome=ModelRun.Outcome.REFUSED, tier=tier,
                prompt_version=prompt_version, content_item=content_item,
                latency_ms=latency_ms, error="Model declined the request", **tokens,
            )
            raise LLMInvalidOutput(f"{purpose}: the model declined this request.")

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            self._record(
                purpose=purpose, outcome=ModelRun.Outcome.INVALID, tier=tier,
                prompt_version=prompt_version, content_item=content_item,
                latency_ms=latency_ms, error="No parsed output on the response", **tokens,
            )
            raise LLMInvalidOutput(f"{purpose}: response carried no schema-valid output.")

        _warn_if_cache_is_a_no_op(purpose, system, tokens)

        self._record(
            purpose=purpose, outcome=ModelRun.Outcome.SUCCEEDED, tier=tier,
            prompt_version=prompt_version, content_item=content_item,
            latency_ms=latency_ms, **tokens,
        )
        return parsed

    # ── Accounting ──────────────────────────────────────────────────────────

    def _record(
        self,
        *,
        purpose: str,
        outcome: str,
        tier: Tier,
        prompt_version: str,
        content_item: Any | None,
        latency_ms: int = 0,
        error: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> ModelRun:
        """One ModelRun and one CostEvent per call, whatever the outcome.

        A capped call costs nothing, so it gets a ModelRun (it is a fact about
        the system) and no CostEvent (it is not a fact about the bill).
        """
        cost = pricing.cost_of(
            tier.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

        run = ModelRun.objects.create(
            purpose=purpose,
            outcome=outcome,
            model=tier.model,
            prompt_version=prompt_version,
            content_item=content_item,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            error=error,
        )

        if cost > Decimal(0):
            costs.record(
                model=tier.model,
                purpose=purpose,
                cost=cost,
                units=tokens_summary(
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
                ),
                run_id=str(run.pk),
            )
        return run


#: Anthropic's minimum cacheable prefix is model-dependent — between 512 and
#: 4096 tokens. Below it, `cache_control` is accepted and silently ignored.
#: Used here only to decide whether a zero-cache result is worth a warning.
MIN_CACHEABLE_TOKENS = 1024


def _warn_if_cache_is_a_no_op(purpose: str, system: Any, tokens: dict) -> None:
    """Say so when a marked prefix cached nothing.

    A prefix shorter than the model's minimum is accepted and ignored — no
    error, no cache, and the `cache_control` in the request is decoration. That
    is a silent waste, and silent is the problem: the code looks like it is
    caching, the bill says otherwise, and nothing connects the two.

    Only fires on a marked prompt that neither wrote nor read, and only for
    prefixes plausibly near the threshold — a long prefix that fails to cache
    is a different bug and deserves a different message.
    """
    marked = isinstance(system, list) and any("cache_control" in b for b in system)
    if not marked:
        return
    if tokens["cache_read_tokens"] or tokens["cache_write_tokens"]:
        return

    approx = len(str(system)) // 4
    if approx < MIN_CACHEABLE_TOKENS:
        logger.warning(
            "%s: the cached prefix is ~%s tokens, below Anthropic's minimum "
            "cacheable prefix, so cache_control did nothing. Caching starts "
            "working once the domain pack is a real taxonomy rather than a "
            "placeholder — until then this call pays full input price.",
            purpose, approx,
        )
    else:
        logger.warning(
            "%s: a %s-token prefix was marked for caching but neither wrote nor "
            "read. Something volatile is in the cached block — a timestamp, an "
            "item id — invalidating it on every call.",
            purpose, approx,
        )


def tokens_summary(inp: int, out: int, cache_read: int, cache_write: int) -> dict:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def cached_system(stable: str, volatile: str = "") -> list[dict]:
    """A system prompt split for caching.

    Everything before the last `cache_control` breakpoint is cached, and a
    single changed byte anywhere in that prefix invalidates all of it. So the
    stable half — instructions, the domain pack taxonomy — is marked, and
    anything that varies per call goes after it, unmarked.

    Getting this backwards is the classic mistake: a timestamp or an item id in
    the cached block means the cache never hits and the premium is paid on
    every call for nothing. `usage.cache_read_input_tokens` on the ModelRun is
    how you check it is working.
    """
    blocks: list[dict] = [
        {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}
    ]
    if volatile:
        blocks.append({"type": "text", "text": volatile})
    return blocks
