from django.apps import AppConfig


class ScoringConfig(AppConfig):
    """L5 · Tenant scoring · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scoring"
    label = "scoring"
    verbose_name = "L5 · Tenant scoring"
