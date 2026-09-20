from django.apps import AppConfig


class OutputsConfig(AppConfig):
    """L6 · Output builder · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.outputs"
    label = "outputs"
    verbose_name = "L6 · Output builder"
