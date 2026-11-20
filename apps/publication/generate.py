"""Building an output's sections from current approved evidence — one path for
the first draft, "Regenerate from current evidence", and nothing else.

Caller binds the tenant scope. Raises `DraftingError` with a reason written for
the operator: nothing approved yet, no recommendation, or the model failed.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from apps.enrichment import promotion
from apps.outputs import briefing, content_brief, opportunity_brief
from apps.outputs.models import OutputType
from apps.outputs.services import DraftingError


@dataclass
class Generated:
    sections: list[dict]
    title: str
    summary: str
    profile: str


def generate(organization, type_: str, *, recommendation=None) -> Generated:
    # One promotional classification for every output (Mark, 15 Nov): a mark
    # made on an observation reaches the claims before anything is counted.
    promotion.reclassify(actor_label="brief generation")
    if type_ == OutputType.CONTENT_BRIEF:
        if recommendation is None:
            raise DraftingError(
                "Choose the approved recommendation the content brief is for — the button "
                "on its Signal Review page does this for you."
            )
        sections, working_title, profile = content_brief.sections_for(
            organization, recommendation
        )
        return Generated(
            sections, working_title, f"Content brief for {recommendation.signal.code}.", profile
        )
    if type_ == OutputType.PRODUCT_MEMO:
        brief = opportunity_brief.sections_for(organization)
        return Generated(
            _with_prose(brief.sections, maturity_of=None),
            f"{organization.name} — new product opportunity brief, {timezone.now():%B %Y}",
            f"{brief.developments} approved candidate(s), in priority order.",
            brief.profile,
        )
    if type_ == OutputType.TREND_BRIEF:
        brief = briefing.sections_for_brief(organization)
        return Generated(
            _with_prose(brief.sections, maturity_of=_maturities()),
            f"{organization.name} — trend brief, {timezone.now():%-d %B %Y}",
            brief.summary,
            brief.profile,
        )
    raise DraftingError(f"{dict(OutputType.choices).get(type_, type_)} has no generator yet.")


def _maturities() -> dict[str, str]:
    from apps.intelligence.models import Signal

    return dict(Signal.objects.filter(state=Signal.State.APPROVED).values_list("title", "maturity"))


def _with_prose(sections: list[dict], *, maturity_of: dict | None) -> list[dict]:
    """A summary and a paragraph per item, written from the brief itself
    (`apps.outputs.narrative`). Without a model, the brief is unchanged."""
    import re

    from apps.outputs import narrative

    items = []
    for section in sections:
        found = re.match(r"^A\d+\. (.+)$", section["heading"])
        if not found:
            continue
        label = found.group(1)
        maturity = None
        if maturity_of is not None:
            maturity = maturity_of.get(label, "watch")
        items.append((section["heading"], label, maturity))
    if not items:
        return sections
    written = narrative.write(sections, items=items)
    return narrative.apply(sections, written, items, summary_heading="Decision summary")
