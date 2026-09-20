"""evidence — L2, GLOBAL.

PRD §8 models: ContentItem, TranscriptArtifact, ContentSegment, MetricSnapshot, Tombstone.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).
"""

from django.db import models  # noqa: F401
