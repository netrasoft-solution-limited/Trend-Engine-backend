#!/usr/bin/env python
"""Django's command-line utility.

Defaults to the OPERATOR settings. Management commands run on the operator
plane, and most of them need operator-wide visibility.

For anything that must run as a tenant, bind the scope explicitly inside the
command with `tenancy.context.scoped(org)` rather than switching settings —
the portal settings are for serving portal requests, not for scripting.
"""
import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.ops")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Couldn't import Django. Is it installed and is your virtual "
            "environment activated?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
