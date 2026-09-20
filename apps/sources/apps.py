from django.apps import AppConfig


class SourcesConfig(AppConfig):
    """L1 · Source registry · GLOBAL."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sources"
    label = "sources"
    verbose_name = "L1 · Source registry"
