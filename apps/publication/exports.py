"""A Publication as a renderable document — the L7 half of the adapter pair.

`apps.outputs` cannot import `Publication`, so the renderer takes a plain
dataclass and each layer builds its own. This is the published side: what a
client is actually sent.

A live publication is never a draft, and a withdrawn one always is. That second
case matters: an operator exporting something they have already withdrawn is
either checking what went out or preparing a correction, and in both cases the
file must not look current.
"""
from __future__ import annotations

from apps.outputs.exports import ExportDocument, Provenance, document_from_body


def document_for_publication(publication) -> ExportDocument:
    return document_from_body(
        title=publication.title,
        type_label=publication.type_label,
        organization_name=publication.organization.name,
        summary=publication.summary,
        body=publication.body,
        is_draft=publication.unpublished_at is not None,
        # The publication date, not the render time — a stable fact about the
        # document, so the same publication always renders to the same bytes.
        dated=publication.published_at.date() if publication.published_at else None,
        provenance=Provenance(
            version_number=publication.version_number,
            client_profile_version=publication.output.client_profile_version,
            domain_pack_version=publication.output.domain_pack_version,
        ),
    )
