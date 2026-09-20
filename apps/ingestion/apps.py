from django.apps import AppConfig


class IngestionConfig(AppConfig):
    """L1-L2 · Run orchestration · GLOBAL."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.ingestion"
    label = "ingestion"
    verbose_name = "L1-L2 · Run orchestration"
