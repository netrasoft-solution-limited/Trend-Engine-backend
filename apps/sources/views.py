"""Source registry — server-rendered views (HTMX partials, no SPA).

The provider credentials screen lives here rather than in Django's admin, and
that is a deliberate correction rather than a preference: `django.contrib.admin`
brings a `LogEntry` with a foreign key to `settings.AUTH_USER_MODEL`, which the
two planes resolve to different identity tables over one shared database. See
the note in `config/settings/base.py` and the guard in
`tests/test_migrations_are_settings_independent.py`.

Three rules hold here, the same three that would have held in the admin:

  · A stored secret is NEVER sent back to the browser. Inputs are always blank;
    blank means "keep what is there". Only a masked hint is displayed.
  · Every change writes an AuditEvent (PRD §7.1) — the value never, the fact
    always.
  · Platform Admins only. PRD §3.2 keeps that role distinct from Operator even
    where one person holds both; reading a provider's caps is an operator task,
    changing the key that spends against them is not.
"""
from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from apps.operations.models import AuditEvent

from .credentials import CredentialUnreadable
from .models import AcquisitionProvider

#: Which named secrets each provider takes. Taddy needs two and authenticates
#: with neither alone, which is why the stored blob is a mapping rather than a
#: single string.
CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    AcquisitionProvider.Kind.APIFY: ("api_key",),
    AcquisitionProvider.Kind.ASSEMBLYAI: ("api_key",),
    AcquisitionProvider.Kind.TADDY: ("api_key", "user_id"),
    AcquisitionProvider.Kind.NCBI: ("api_key",),
    AcquisitionProvider.Kind.CROSSREF: ("api_key",),
}

DEFAULT_FIELDS = ("api_key",)


def fields_for(kind: str) -> tuple[str, ...]:
    return CREDENTIAL_FIELDS.get(kind, DEFAULT_FIELDS)


def _require_platform_admin(request) -> None:
    user = request.user
    allowed = getattr(user, "is_superuser", False) or getattr(user, "role", "") == "platform_admin"
    if not allowed:
        raise PermissionDenied(
            "Changing provider credentials requires the Platform Admin role."
        )


def _describe(provider: AcquisitionProvider) -> dict:
    """One provider, as the template needs it — and never with a secret in it.

    `inputs` is a list of pairs rather than a mapping because a Django template
    cannot look a dict up by a loop variable, and the silent result of trying is
    an empty string where the environment variable name should be.
    """
    return {
        "obj": provider,
        "inputs": [
            {"name": name, "env_var": provider.default_env_var(name)}
            for name in fields_for(provider.kind)
        ],
        "primary_env_var": provider.default_env_var(),
    }


class ProviderListView(View):
    """Every acquisition provider, with the state of its credentials."""

    def get(self, request):
        _require_platform_admin(request)
        providers = [_describe(p) for p in AcquisitionProvider.objects.all()]
        return render(
            request,
            "ops/providers.html",
            {"providers": providers, "kinds": AcquisitionProvider.Kind.choices},
        )


class ProviderCredentialsView(View):
    """Set or clear one provider's secrets."""

    def post(self, request, pk: int):
        _require_platform_admin(request)
        provider = get_object_or_404(AcquisitionProvider, pk=pk)
        actor = getattr(request.user, "email", str(request.user))
        expected = fields_for(provider.kind)

        if request.POST.get("clear"):
            provider.set_credentials({}, actor_label=actor)
            provider.save()
            _audit(request, provider, "cleared", [])
            messages.info(
                request,
                f"{provider.name} credentials removed. It will fall back to "
                f"{provider.default_env_var()} if that is set in the environment.",
            )
            return redirect(reverse("ops-providers"))

        supplied = {name: (request.POST.get(name) or "").strip() for name in expected}

        if not any(supplied.values()):
            messages.warning(request, "Nothing entered — credentials unchanged.")
            return redirect(reverse("ops-providers"))

        # Refuse a half-entered pair rather than storing one of two values and
        # leaving the operator to discover it later as a 401 in a log.
        if len(expected) > 1 and not all(supplied.values()):
            missing = ", ".join(n for n, v in supplied.items() if not v)
            messages.error(
                request,
                f"{provider.get_kind_display()} authenticates with "
                f"{' and '.join(expected)}. Missing: {missing}. Enter both or neither.",
            )
            return redirect(reverse("ops-providers"))

        had_one = provider.has_credential
        provider.set_credentials(supplied, actor_label=actor)
        provider.save()

        action = "replaced" if had_one else "set"
        _audit(request, provider, action, sorted(n for n, v in supplied.items() if v))
        messages.success(
            request,
            f"{provider.name} credentials {action}. This takes effect on the next "
            f"run — nothing needs redeploying.",
        )
        return redirect(reverse("ops-providers"))


def _audit(request, provider: AcquisitionProvider, action: str, secrets: list[str]) -> None:
    """The fact, never the value.

    PRD §7.1 requires configuration events in the audit log, and a credential
    change is the one most worth having — it is what explains why a connector
    started or stopped working.
    """
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_id=getattr(request.user, "pk", None),
        actor_label=getattr(request.user, "email", str(request.user)),
        message=f"{provider.name} credentials {action}",
        context={
            "provider": provider.kind,
            "action": action,
            "secrets": secrets,
            "hint": provider.credential_hint,
        },
    )


def provider_health(provider: AcquisitionProvider) -> str:
    """Where this provider's key is coming from, in words.

    Reads the credential, so it surfaces an unreadable one — the case where the
    encryption key has changed and every stored secret is silently dead.
    """
    try:
        if provider.has_credential:
            return "stored"
    except CredentialUnreadable:
        return "unreadable"
    return "environment" if provider.credential() else "missing"
