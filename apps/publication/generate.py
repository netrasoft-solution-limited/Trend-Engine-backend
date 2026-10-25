"""Building an output's sections from current approved evidence — one path for
the first draft, "Regenerate from current evidence", and nothing else.

Caller binds the tenant scope. Raises `DraftingError` with a reason written for
the operator: nothing approved yet, no recommendation, or the model failed.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

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
            brief.sections,
            f"{organization.name} — new product opportunity brief, {timezone.now():%B %Y}",
            f"{brief.developments} approved candidate(s), in priority order.",
            brief.profile,
        )
    if type_ == OutputType.TREND_BRIEF:
        brief = briefing.sections_for_brief(organization)
        return Generated(
            brief.sections,
            f"{organization.name} — trend brief, {timezone.now():%-d %B %Y}",
            brief.summary,
            brief.profile,
        )
    raise DraftingError(f"{dict(OutputType.choices).get(type_, type_)} has no generator yet.")
