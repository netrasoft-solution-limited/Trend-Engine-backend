from django.apps import AppConfig


class ConnectorsConfig(AppConfig):
    """L1 · Connector adapters · GLOBAL."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.connectors"
    label = "connectors"
    verbose_name = "L1 · Connector adapters"
