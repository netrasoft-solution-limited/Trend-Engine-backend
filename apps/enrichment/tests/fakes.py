"""A model that never leaves the process.

The whole AI layer is testable without an API key, without a network and
without spending anything. That is not only convenience: a suite that calls a
real model is non-deterministic, costs money per run, and cannot assert on the
refusal and cap paths at all, because you cannot ask a real API to be over
budget on demand.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeUsage:
    input_tokens: int = 1200
    output_tokens: int = 150
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeResponse:
    parsed_output: Any = None
    usage: FakeUsage = field(default_factory=FakeUsage)
    stop_reason: str = "end_turn"


class FakeMessages:
    def __init__(self, outer: "FakeTransport") -> None:
        self._outer = outer

    def parse(self, **kwargs) -> FakeResponse:
        self._outer.calls.append(kwargs)
        if self._outer.raises is not None:
            raise self._outer.raises
        response = self._outer.responses.pop(0) if self._outer.responses else FakeResponse()
        return response


class FakeTransport:
    """Stands in for `anthropic.Anthropic()`.

    `calls` records every request, so a test can assert on what was actually
    sent — which is how the "the gate never sees the transcript" rule is
    checked rather than assumed.
    """

    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[dict] = []
        self.messages = FakeMessages(self)


def respond(parsed: Any, **usage) -> FakeResponse:
    return FakeResponse(parsed_output=parsed, usage=FakeUsage(**usage))
