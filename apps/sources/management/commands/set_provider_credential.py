"""Set a provider's credentials from the command line.

The screen at `/ops/sources/providers/` is the normal route. This exists for
the cases the screen cannot serve: the first deploy, before anyone has an
operator account; a rotation done over SSH during an incident; and CI.

The secret is read from a PROMPT or from an environment variable, never from an
argument. A key passed as `--api-key sk-...` is in the shell history, in `ps`
output for every user on the box, and in any process-listing the monitoring
agent collects.
"""
from __future__ import annotations

import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from apps.operations.models import AuditEvent
from apps.sources.models import AcquisitionProvider
from apps.sources.views import fields_for


class Command(BaseCommand):
    help = "Store or clear an acquisition provider's credentials (encrypted)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "kind",
            choices=[k for k, _ in AcquisitionProvider.Kind.choices],
            help="Which provider, e.g. apify, assemblyai, taddy.",
        )
        parser.add_argument(
            "--from-env",
            action="store_true",
            help=(
                "Read each secret from its conventional environment variable "
                "(APIFY_TOKEN, ASSEMBLYAI_API_KEY, …) instead of prompting. "
                "For the first deploy, moving what is already in the "
                "environment into the database."
            ),
        )
        parser.add_argument(
            "--clear", action="store_true", help="Remove the stored credentials."
        )
        parser.add_argument(
            "--actor",
            default="",
            help="Who to record in the audit log. Defaults to the shell user.",
        )

    def handle(self, *args, **options) -> None:
        kind = options["kind"]
        actor = options["actor"] or f"{getpass.getuser()} (shell)"

        provider, created = AcquisitionProvider.objects.get_or_create(
            kind=kind, defaults={"name": dict(AcquisitionProvider.Kind.choices)[kind]}
        )
        if created:
            self.stdout.write(f"Registered provider {provider.name}.")

        if options["clear"]:
            provider.set_credentials({}, actor_label=actor)
            provider.save()
            self._audit(provider, "cleared", [], actor)
            self.stdout.write(
                self.style.WARNING(
                    f"{provider.name} credentials removed. It will fall back to "
                    f"{provider.default_env_var()} if that is set."
                )
            )
            return

        names = fields_for(kind)
        values: dict[str, str] = {}

        for name in names:
            variable = provider.default_env_var(name)
            if options["from_env"]:
                value = os.environ.get(variable, "")
                if not value:
                    raise CommandError(
                        f"{variable} is not set, so there is nothing to import for "
                        f"'{name}'. Run without --from-env to enter it directly."
                    )
            else:
                value = getpass.getpass(f"{provider.name} {name}: ").strip()

            if not value:
                raise CommandError(
                    f"No value given for '{name}'. {provider.name} authenticates "
                    f"with {' and '.join(names)} — storing a partial set would "
                    f"fail at the next run for a reason nobody can see."
                )
            values[name] = value

        had_one = provider.has_credential
        provider.set_credentials(values, actor_label=actor)
        provider.save()

        action = "replaced" if had_one else "set"
        self._audit(provider, action, sorted(values), actor)
        self.stdout.write(
            self.style.SUCCESS(
                f"{provider.name} credentials {action} — {provider.credential_hint}. "
                f"Takes effect on the next run; nothing needs redeploying."
            )
        )

    def _audit(
        self, provider: AcquisitionProvider, action: str, secrets: list[str], actor: str
    ) -> None:
        AuditEvent.objects.create(
            kind=AuditEvent.Kind.CONFIG,
            actor_realm=AuditEvent.Realm.SYSTEM,
            actor_label=actor,
            message=f"{provider.name} credentials {action} from the command line",
            context={
                "provider": provider.kind,
                "action": action,
                "secrets": secrets,
                "hint": provider.credential_hint,
            },
        )
