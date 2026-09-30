"""There is no Django admin in this project, deliberately.

`django.contrib.admin` ships a `LogEntry` with a foreign key to
`settings.AUTH_USER_MODEL`. The two planes resolve that setting differently —
`operations.OperatorUser` on the operator plane, `portal.OrgUser` on the portal
plane — over ONE shared database, so the same `django_admin_log.user_id` column
would point at a different identity table depending on which process ran
`migrate`. That is precisely the silent cross-realm join
`apps/operations/models.py` forbids.

It was tried, and it is not theoretical: with the admin installed, the database
had that constraint pointing at `operations_operatoruser` while the portal
registry believed it pointed at `portal_orguser`. Both `makemigrations --check`
runs were clean, because a swappable dependency is settings-dependent by design
and reports no changes under either module.

Provider credentials — the thing the admin was wanted for — have their own
operator screen in `apps/sources/views.py`, with the same three guarantees:
secrets are never rendered back, every change is audited, and only Platform
Admins may make one. `tests/test_migrations_are_settings_independent.py` fails
if anything reintroduces a swappable dependency.
"""
