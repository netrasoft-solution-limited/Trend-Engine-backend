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

from . import registry
from .credentials import CredentialUnreadable
from .models import AcquisitionProvider, ProviderPolicyVersion, Source

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


# ── Source registry (PRD §6.5) ──────────────────────────────────────────────
#
# Which shows and channels are watched, and how often. Before this screen it was
# a management command, which meant only whoever had a terminal could answer
# "can we start watching this podcast?".
#
# Two things the screen is responsible for that a shell was not:
#
#   · Refusing a source that cannot collect. A `Source` with the wrong config
#     keys saves perfectly and then silently returns nothing forever — the
#     failure mode that looks like a quiet category rather than a broken
#     source. `registry.config_problem()` is checked before save and shown on
#     every row afterwards.
#   · Making the access basis visible. PRD §7.2: no connector runs without a
#     recorded policy. `Source.can_collect` already enforces it; the screen is
#     what stops an operator wondering why their new feed does nothing.


class SourceListView(View):
    """Every source, what it is set to collect, and whether it can."""

    def get(self, request):
        _require_platform_admin(request)

        rows = []
        for source in Source.objects.select_related("policy", "policy__provider").all():
            rows.append(
                {
                    "obj": source,
                    "summary": registry.describe_config(source.route, source.config),
                    "problem": registry.config_problem(source.route, source.config),
                }
            )

        live = _live_policies()
        return render(
            request,
            "ops/sources/sources.html",
            {
                "sources": rows,
                "specs": [
                    {"spec": spec, "policies": registry.policies_for(spec.route, live)}
                    for spec in registry.available_specs()
                ],
                "blocked": [s for s in registry.SPECS.values() if not s.available],
                "policies": live,
            },
        )

    def post(self, request):
        """Add a source."""
        _require_platform_admin(request)

        name = (request.POST.get("name") or "").strip()
        route = request.POST.get("route") or ""
        spec = registry.spec_for(route)

        if not name:
            messages.error(request, "A source needs a name you will recognise later.")
            return redirect(reverse("ops-sources"))
        if spec is None or not spec.available:
            messages.error(
                request,
                spec.unavailable_because if spec else f"“{route}” is not a route.",
            )
            return redirect(reverse("ops-sources"))

        config = _config_from(request, spec)
        problem = registry.config_problem(route, config)
        if problem:
            # Refused rather than saved-and-broken: a source that collects
            # nothing is indistinguishable from a quiet week.
            messages.error(request, problem)
            return redirect(reverse("ops-sources"))

        policy = _policy_from(request)
        if policy is None:
            messages.error(
                request,
                "Choose the access basis this source collects under. PRD §7.2: no "
                "connector runs without one, so a source saved without it would "
                "never collect.",
            )
            return redirect(reverse("ops-sources"))

        # Checked here as well as filtered in the form. A dropdown that only
        # offers the right answers is a convenience; this is the rule.
        if policy not in registry.policies_for(route, [policy]):
            messages.error(
                request,
                f"{policy.provider.name} does not collect {spec.label.lower()} "
                f"sources. The access basis has to be the one belonging to the "
                f"vendor that actually fetches this, or the record is wrong in a "
                f"way nothing later would catch.",
            )
            return redirect(reverse("ops-sources"))

        if Source.objects.filter(name=name).exists():
            messages.error(request, f"A source is already called “{name}”.")
            return redirect(reverse("ops-sources"))

        source = Source.objects.create(
            name=name,
            route=route,
            config=config,
            policy=policy,
            poll_interval_minutes=_interval_from(request),
        )
        _audit_source(request, source, "added")
        messages.success(
            request,
            f"{name} added. It will be collected within the hour and then on its "
            f"own schedule — or poll it now from its page.",
        )
        return redirect(reverse("ops-source", args=[source.pk]))


class SourceDetailView(View):
    """One source: change what it watches, how often, and whether it runs."""

    def get(self, request, pk: int):
        _require_platform_admin(request)
        source = get_object_or_404(Source, pk=pk)
        spec = registry.spec_for(source.route)

        return render(
            request,
            "ops/sources/source.html",
            {
                "source": source,
                "spec": spec,
                "values": _values_for(spec, source.config),
                "problem": registry.config_problem(source.route, source.config),
                "policies": registry.policies_for(source.route, _live_policies()),
            },
        )

    def post(self, request, pk: int):
        _require_platform_admin(request)
        source = get_object_or_404(Source, pk=pk)
        action = request.POST.get("action") or "save"

        if action == "pause":
            source.status = Source.Status.FAILED
            source.save(update_fields=["status"])
            _audit_source(request, source, "paused")
            messages.info(request, f"{source.name} paused. Nothing will be collected from it.")
            return redirect(reverse("ops-source", args=[pk]))

        if action == "resume":
            source.status = Source.Status.ACTIVE
            source.save(update_fields=["status"])
            _audit_source(request, source, "resumed")
            messages.success(request, f"{source.name} is collecting again.")
            return redirect(reverse("ops-source", args=[pk]))

        if action == "poll":
            if not source.can_collect:
                messages.error(
                    request,
                    f"{source.name} cannot collect: "
                    + ("it is paused. " if source.status == Source.Status.FAILED else "")
                    + ("it has no live access basis." if not (source.policy and source.policy.is_live) else ""),
                )
                return redirect(reverse("ops-source", args=[pk]))
            # By name, not by import: `apps.evidence` sits above `apps.sources`
            # in the layer contract. This is the same seam `apps/evidence/tasks.py`
            # uses to reach enrichment — the queue is the boundary, so a task can
            # be dispatched upward without the dependency travelling with it.
            from celery import current_app

            current_app.send_task(
                "apps.evidence.tasks.poll_source", args=[source.pk], queue="ingest"
            )
            messages.info(
                request,
                f"Polling {source.name} now. New items appear within a minute or two; "
                f"what arrives is screened for relevance before anything is transcribed.",
            )
            return redirect(reverse("ops-source", args=[pk]))

        # Save.
        spec = registry.spec_for(source.route)
        if spec is None or not spec.available:
            messages.error(request, "This source's route can no longer be configured.")
            return redirect(reverse("ops-source", args=[pk]))

        config = _config_from(request, spec)
        problem = registry.config_problem(source.route, config)
        if problem:
            messages.error(request, problem)
            return redirect(reverse("ops-source", args=[pk]))

        policy = _policy_from(request) or source.policy
        source.config = config
        source.policy = policy
        source.poll_interval_minutes = _interval_from(request)
        source.name = (request.POST.get("name") or source.name).strip()
        source.save(update_fields=["config", "policy", "poll_interval_minutes", "name"])
        _audit_source(request, source, "changed")
        messages.success(request, f"{source.name} saved.")
        return redirect(reverse("ops-source", args=[pk]))


def _config_from(request, spec) -> dict:
    """Form fields back into the JSON the collector reads.

    List fields are one comma-separated input. Asking an operator to type JSON
    into a form makes them responsible for the schema, and a trailing comma
    becomes a 500.
    """
    config: dict = {}
    for field in spec.fields:
        raw = (request.POST.get(f"config_{field.key}") or "").strip()
        if not raw:
            continue
        if field.is_list:
            values = [part.strip() for part in raw.split(",") if part.strip()]
            if values:
                config[field.key] = values
        else:
            config[field.key] = raw
    return config


def _values_for(spec, config: dict) -> list[dict]:
    """The stored config rendered back into form values."""
    if spec is None:
        return []
    rendered = []
    for field in spec.fields:
        value = (config or {}).get(field.key)
        if field.is_list and isinstance(value, list):
            value = ", ".join(value)
        rendered.append({"field": field, "value": value or ""})
    return rendered


def _interval_from(request) -> int:
    """Hours in the form, minutes in the database.

    Zero is meaningful and kept: it means "never automatically", which is how a
    source polled only by hand behaves.
    """
    try:
        hours = max(0, min(24 * 30, int(request.POST.get("interval_hours") or 24)))
    except (TypeError, ValueError):
        hours = 24
    return hours * 60


def _policy_from(request):
    policy_id = request.POST.get("policy")
    if not policy_id:
        return None
    policy = ProviderPolicyVersion.objects.filter(pk=policy_id).first()
    return policy if policy is not None and policy.is_live else None


def _live_policies() -> list:
    return [
        policy
        for policy in ProviderPolicyVersion.objects.select_related("provider").all()
        if policy.is_live
    ]


def _audit_source(request, source, action: str) -> None:
    AuditEvent.objects.create(
        kind=AuditEvent.Kind.CONFIG,
        actor_realm=AuditEvent.Realm.OPERATOR,
        actor_id=getattr(request.user, "pk", None),
        actor_label=getattr(request.user, "email", str(request.user)),
        message=f"Source {source.name} {action}",
        context={
            "source_id": source.pk,
            "route": source.route,
            "action": action,
            "config_keys": sorted(source.config or {}),
            "poll_interval_minutes": source.poll_interval_minutes,
        },
    )
