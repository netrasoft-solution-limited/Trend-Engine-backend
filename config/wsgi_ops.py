"""WSGI entry point for the operator plane.

Arch §11.1: two WSGI processes, one codebase. Separate entry points give the
planes independent middleware stacks and make it impossible for portal traffic
to load operator URL routing at all. Cost: one extra lightweight process.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.ops")

application = get_wsgi_application()
