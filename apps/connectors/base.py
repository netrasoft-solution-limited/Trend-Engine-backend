"""The connector adapter contract — Arch §7.1.

Every source, however acquired, implements this one interface. Driver #5:
vendors are replaceable, our data is not. Swapping an Apify Actor, or dropping
Apify entirely for a platform, must touch configuration and nothing downstream.

Two rules are encoded here rather than left to convention:

    · No policy, no run. A connector without a ProviderPolicyVersion cannot be
      constructed (PRD §7.2).
    · Capability flags decide what a connector is allowed to claim. A source
      without TRANSCRIPT produces metadata-only items, and they must surface
      honestly as such rather than as fully understood content (Arch §7.1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Iterable, Protocol, runtime_checkable


class Capability(str, Enum):
    DISCOVERY = "DISCOVERY"
    METADATA = "METADATA"
    TRANSCRIPT = "TRANSCRIPT"
    ENGAGEMENT = "ENGAGEMENT"
    COMMENTS = "COMMENTS"
    DELETION_POLL = "DELETION_POLL"


@dataclass(frozen=True)
class DateWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class ItemRef:
    """A discovered item, before anything has been fetched."""

    source_id: int
    external_item_id: str
    url: str | None = None


@dataclass(frozen=True)
class CostEstimate:
    """What a fetch plan will cost, computed BEFORE the run starts.

    Arch §10.2: hard caps are enforced ahead of the run, not reconciled after.
    A runaway Actor cannot silently consume a month's margin.
    """

    currency: str
    amount: Decimal
    units: dict[str, float]


@dataclass(frozen=True)
class FetchPlan:
    refs: list[ItemRef]
    want: set[Capability]


@dataclass(frozen=True)
class ContentResult:
    """The outcome of one content fetch, including the route that produced it.

    `route` is the fallback-chain step that succeeded, so transcript provenance
    and cost stay attributable per item (Arch §7.2).
    """

    text: str | None
    route: str
    is_metadata_only: bool
    raw_checksum: str


@runtime_checkable
class Connector(Protocol):
    """See Arch §7.1 for the canonical form of this protocol."""

    capabilities: set[Capability]
    #: Access basis. No policy, no run — enforced in `BaseConnector.__init__`.
    policy: "object"

    def discover(self, window: DateWindow) -> Iterable[ItemRef]: ...

    def fetch_metadata(self, refs: list[ItemRef]) -> Iterable["object"]: ...

    def fetch_content(self, ref: ItemRef) -> ContentResult: ...

    def estimate_cost(self, plan: FetchPlan) -> CostEstimate: ...


class ConnectorPolicyError(RuntimeError):
    """Raised when a connector is constructed or run without an access basis."""


class BaseConnector:
    """Shared enforcement. Concrete adapters live in `providers/`.

    Subclasses declare `capabilities` and are handed a policy at construction.
    There is intentionally no default policy and no `policy=None` path.
    """

    capabilities: set[Capability] = set()

    def __init__(self, *, source, policy) -> None:
        if policy is None:
            raise ConnectorPolicyError(
                f"{type(self).__name__} for source {source!r} has no ProviderPolicyVersion. "
                f"PRD §7.2: no connector runs without a recorded provider policy "
                f"and access basis."
            )
        self.source = source
        self.policy = policy

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def produces_metadata_only(self) -> bool:
        """True when this connector cannot supply full text.

        Callers must propagate this onto the evidence record. An item that is
        metadata-only is excluded from claim extraction and is labelled as such
        everywhere it appears.
        """
        return not self.supports(Capability.TRANSCRIPT)
