from django.apps import AppConfig


class BillingConfig(AppConfig):
    """L7 · Subscriptions · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"
    verbose_name = "L7 · Subscriptions"
