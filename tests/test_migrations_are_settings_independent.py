"""One migration history, two AUTH_USER_MODELs — the invariant that allows it.

The operator plane resolves `AUTH_USER_MODEL` to `operations.OperatorUser` and
the portal plane to `portal.OrgUser`, over ONE database and ONE migration
history. That is only coherent while no migration contains a reference to the
setting: Django resolves such a reference at import time, so the same column
would point at `operations_operatoruser` in one process and `portal_orguser` in
the other — two identity tables silently joined on the same integer key.

CI already runs `makemigrations --check` under both settings modules, and that
check has a BLIND SPOT this file exists to cover. A swappable dependency is
settings-dependent *by design*: Django emits it deliberately and reports "no
changes detected" under either module, so the existing check passes while the
schema quietly diverges by whichever plane ran `migrate` first.

This was not hypothetical. Adding `django.contrib.admin` — whose `LogEntry` has
a user foreign key — produced exactly that: a `django_admin_log.user_id`
constraint pointing at `operations_operatoruser` in the database while the
portal registry believed it pointed at `portal_orguser`. Both `--check` runs
were clean. Hence this test.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

#: Every migration in the project's own apps. Third-party migrations are not
#: ours to change; the point is to refuse an app whose migrations would carry
#: such a dependency, which is a decision made in INSTALLED_APPS.
MIGRATIONS = sorted(BACKEND.glob("apps/*/migrations/0*.py"))


def test_there_are_migrations_to_check():
    """Guards against this whole file passing because the glob found nothing."""
    assert len(MIGRATIONS) > 5, f"only found {len(MIGRATIONS)} migrations — check the glob"


def _setting_references(source: str) -> tuple[bool, bool]:
    """(references AUTH_USER_MODEL, declares a swappable dependency).

    Parsed rather than grepped: several files in this tree explain the rule in
    a comment, and a comment describing the hazard is not the hazard.
    """
    tree = ast.parse(source)
    references = swappable = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "AUTH_USER_MODEL":
            references = True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "swappable_dependency":
                swappable = True

    return references, swappable


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: f"{p.parent.parent.name}/{p.name}")
def test_no_migration_references_the_user_model_setting(path: Path):
    """No `settings.AUTH_USER_MODEL`, and no `swappable_dependency`.

    Both compile to the same hazard. A model needing a user foreign key names
    its concrete target as a string literal — see `apps/operations/models.py`,
    where `AuditEvent` carries a denormalised actor for this reason among
    others.
    """
    references, swappable = _setting_references(path.read_text())

    assert not references, (
        f"{path.relative_to(BACKEND)} references settings.AUTH_USER_MODEL. "
        f"Django resolves that at import time, so this column would point at a "
        f"different identity table in each plane. Name the concrete model as a "
        f"string literal instead."
    )
    assert not swappable, (
        f"{path.relative_to(BACKEND)} declares a swappable dependency, which is "
        f"the same hazard in a different shape — and one the dual "
        f"`makemigrations --check` in CI does NOT catch."
    )


def test_no_model_field_uses_the_setting():
    """The source-level half, so the rule is visible before a migration exists.

    Catching it only at migration time means discovering it after someone has
    already written the model and run `makemigrations`.
    """
    offenders = []
    for models_file in sorted(BACKEND.glob("apps/*/models.py")):
        source = models_file.read_text()
        if "AUTH_USER_MODEL" not in source:
            continue
        # A docstring or comment explaining the rule is not a violation; a real
        # reference parses as an attribute access.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "AUTH_USER_MODEL":
                offenders.append(str(models_file.relative_to(BACKEND)))
                break

    assert not offenders, (
        f"These models reference settings.AUTH_USER_MODEL: {offenders}. "
        f"Name the concrete user model as a string literal — the two planes "
        f"share one migration history and would resolve it differently."
    )


@pytest.mark.django_db
def test_no_app_in_the_project_brings_a_swappable_dependency():
    """The check that would actually have caught the admin.

    The file scan above only sees `apps/*/migrations`. Django's own apps live
    in site-packages, so a third-party app carrying a user foreign key slips
    straight past it — which is exactly what `django.contrib.admin` did.

    This inspects the whole loaded migration graph instead, so the rule follows
    from what is in INSTALLED_APPS rather than from what happens to be in the
    repository.

    `migrations.swappable_dependency(...)` returns a `SwappableTuple` that looks
    like an ordinary `(app_label, "__first__")` pair — the setting it came from
    survives only on a `.setting` attribute. That attribute is the tell, and
    comparing the tuple itself finds nothing.
    """
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    offenders = {
        f"{app}.{name}: {dependency.setting}"
        for (app, name), migration in loader.disk_migrations.items()
        for dependency in migration.dependencies
        if hasattr(dependency, "setting")
    }

    assert not offenders, (
        f"These migrations depend on a swappable setting: {sorted(offenders)}. "
        f"Each creates a column whose foreign key resolves to a DIFFERENT table "
        f"in each plane — `operations_operatoruser` under ops settings and "
        f"`portal_orguser` under portal settings — over one shared database. "
        f"The dual `makemigrations --check` reports no changes under either, so "
        f"this is the only place it is caught. Remove the app from "
        f"INSTALLED_APPS, or give it a user reference that is a string literal."
    )


def test_installed_apps_is_identical_on_both_planes():
    """The registries must match, or the two `migrate` plans diverge.

    Once they diverge, asserting that both settings modules produce the same
    schema becomes impossible — which is the assertion the whole two-realm
    design rests on.
    """
    from config.settings import ops, portal

    assert list(ops.INSTALLED_APPS) == list(portal.INSTALLED_APPS)
