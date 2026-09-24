from django.apps import AppConfig


class PortalConfig(AppConfig):
    """L7 · Client portal · TENANT-SCOPED."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.portal"
    label = "portal"
    verbose_name = "L7 · Client portal"

    def ready(self) -> None:
        # Connects the publication-gate receivers. `apps.publication` cannot
        # import this app (it sits below it), so the wiring happens here.
        from . import receivers  # noqa: F401
        from .notifications import warn_if_emails_go_to_the_log

        warn_if_emails_go_to_the_log()
