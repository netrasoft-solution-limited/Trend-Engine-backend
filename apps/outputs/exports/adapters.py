"""An OutputVersion as a renderable document. The only file here touching models.

Kept separate so that "does the renderer depend on the ORM?" is answerable by
reading one short file. `apps/publication/exports.py` is the mirror of this at
L7, for a Publication.
"""
from __future__ import annotations

from .document import ExportDocument, Provenance, document_from_body


def document_for_version(version, *, is_draft: bool | None = None) -> ExportDocument:
    """Render-ready, from an `outputs.OutputVersion`.

    `is_draft` defaults to TRUE unless the version is published. Defaulting the
    other way would mean a new state, or a bug, silently producing an
    unwatermarked client-ready file from unapproved content.
    """
    from apps.outputs.models import OutputState

    output = version.output
    if is_draft is None:
        is_draft = version.state != OutputState.PUBLISHED

    return document_from_body(
        title=output.title,
        type_label=output.get_type_display(),
        organization_name=version.organization.name,
        summary=version.summary,
        body=version.body,
        is_draft=is_draft,
        dated=None,
        provenance=Provenance(
            version_number=version.number,
            client_profile_version=output.client_profile_version,
            domain_pack_version=output.domain_pack_version,
        ),
    )
