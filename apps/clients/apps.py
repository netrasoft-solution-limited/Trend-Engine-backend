from django.apps import AppConfig


class ClientsConfig(AppConfig):
    """L5 · Client profiles · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.clients"
    label = "clients"
    verbose_name = "L5 · Client profiles"
