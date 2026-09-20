from django.apps import AppConfig


class TenancyConfig(AppConfig):
    """L0 · Tenant root & scoping · GLOBAL."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenancy"
    label = "tenancy"
    verbose_name = "L0 · Tenant root & scoping"
