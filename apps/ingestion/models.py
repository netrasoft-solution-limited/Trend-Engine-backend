"""ingestion — L1-L2, GLOBAL.

PRD §8 models: IngestionRun, RawItem.

Tenant-agnostic by design. Nothing in this app may import from
clients, scoring, outputs, publication, portal or billing (Arch §4).
"""

from django.db import models  # noqa: F401
