"""enrichment — L3-L4, GLOBAL.

PRD §8 models: Entity, DomainEntity, EntityMention, Topic, TopicMention, Claim, Question, Relationship, ModelRun, DomainAnalysisRun.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).
"""

from django.db import models  # noqa: F401
