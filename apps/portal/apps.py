from django.apps import AppConfig


class PortalConfig(AppConfig):
    """L7 · Client portal · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
    label = "portal"
    verbose_name = "L7 · Client portal"
