"""WSGI entry point for the tenant plane.

This process never loads `config.urls_ops`. That is the structural half of the
escalation guarantee in Arch §5.3 — the permission checks are the other half,
and neither is trusted alone.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.portal")

application = get_wsgi_application()
