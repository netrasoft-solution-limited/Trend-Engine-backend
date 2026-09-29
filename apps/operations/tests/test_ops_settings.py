"""Production must never allow a password-only operator login.

Each settings module is imported in a fresh interpreter with a production-like
environment (DEBUG off), because the value is decided at import time from the
environment, and the running test process has already imported its settings.

No database needed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SETTINGS_DIR = ROOT / "config" / "settings"

#: Every settings module that decides OPERATOR_REQUIRE_MFA. Found by scanning,
#: so a new settings file (a `prod.py`, say) is covered without editing this.
MODULES = sorted(
    f"config.settings.{path.stem}"
    for path in SETTINGS_DIR.glob("*.py")
    if "OPERATOR_REQUIRE_MFA" in path.read_text(encoding="utf-8")
)


def resolve(module: str, **env: str) -> bool:
    """OPERATOR_REQUIRE_MFA as `module` computes it under `env`."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_DEBUG", "OPS_PASSWORD_ONLY_LOGIN"}
    }
    environment.update(
        {"DJANGO_SECRET_KEY": "test-only", "POSTGRES_PASSWORD": "test-only", **env}
    )
    result = subprocess.run(
        [sys.executable, "-c", f"import {module} as s; print(s.OPERATOR_REQUIRE_MFA)"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "True"


def test_the_ops_settings_module_is_among_those_checked():
    assert "config.settings.ops" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_mfa_is_required_with_debug_off(module):
    assert resolve(module, DJANGO_DEBUG="0") is True


@pytest.mark.parametrize("module", MODULES)
def test_the_dev_switch_is_ignored_with_debug_off(module):
    """Setting the escape hatch in a production environment changes nothing."""
    assert resolve(module, DJANGO_DEBUG="0", OPS_PASSWORD_ONLY_LOGIN="1") is True


def test_the_dev_switch_works_with_debug_on():
    """Proves the two tests above would notice if the guard stopped working."""
    assert resolve("config.settings.ops", DJANGO_DEBUG="1", OPS_PASSWORD_ONLY_LOGIN="1") is False
