from django.apps import AppConfig


class EnrichmentConfig(AppConfig):
    """L3-L4 · Extraction & embeddings · GLOBAL."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.enrichment"
    label = "enrichment"
    verbose_name = "L3-L4 · Extraction & embeddings"
