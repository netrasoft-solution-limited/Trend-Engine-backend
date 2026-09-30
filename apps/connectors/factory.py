"""Building a connector for a source, with its credential and its policy.

Every connector needs three things it should not go looking for itself: the
provider's key, the approved access basis, and the caps it may spend against.
Assembling those in one place means a caller cannot accidentally skip the
policy check — `PRD §7.2`'s "no connector runs without a recorded provider
policy and access basis" holds because there is one door.

The credential comes from `AcquisitionProvider.credential()`, which reads the
encrypted database value first and the environment second. That order is the
point of storing them: an environment variable that silently won over a freshly
rotated key would make the operator screen a lie.
"""
from __future__ import annotations

from apps.sources.models import AcquisitionProvider, ProviderPolicyVersion, Source

from .providers.apify import YOUTUBE, ActorConfig, ApifyActorConnector
from .providers.assemblyai import DEFAULT_MODEL, AssemblyAIConnector
from .providers.taddy import TaddyConnector


class ConnectorUnavailable(RuntimeError):
    """A connector cannot be built, and why.

    Distinct from a failed run: this means the system is not configured to make
    the attempt. The message names the fix, because the operator reading it in
    a run log is the person who has to apply it.
    """


def _live_policy(provider: AcquisitionProvider) -> ProviderPolicyVersion:
    policy = (
        ProviderPolicyVersion.objects.filter(
            provider=provider, approved_at__isnull=False, withdrawn_at__isnull=True
        )
        .order_by("-approved_at")
        .first()
    )
    if policy is None:
        raise ConnectorUnavailable(
            f"{provider.name} has no approved provider policy. PRD §7.2: no "
            f"connector runs without a recorded access basis. Record and approve "
            f"one before collecting."
        )
    return policy


def _provider(kind: str) -> AcquisitionProvider:
    provider = AcquisitionProvider.objects.filter(kind=kind).first()
    if provider is None:
        raise ConnectorUnavailable(
            f"No acquisition provider registered for '{kind}'. Add it at "
            f"/ops/sources/providers/ or with `manage.py set_provider_credential {kind}`."
        )
    if provider.paused_at is not None:
        raise ConnectorUnavailable(
            f"{provider.name} is paused since {provider.paused_at:%Y-%m-%d}, "
            f"normally because it reached its monthly cap. An operator clears "
            f"this deliberately."
        )
    return provider


def _credential(provider: AcquisitionProvider, name: str = "api_key") -> str:
    value = provider.credential(name)
    if not value:
        raise ConnectorUnavailable(
            f"{provider.name} has no '{name}' credential. Set it at "
            f"/ops/sources/providers/, or export {provider.default_env_var(name)}."
        )
    return value


def apify_for(source: Source, *, config: ActorConfig = YOUTUBE) -> ApifyActorConnector:
    provider = _provider(AcquisitionProvider.Kind.APIFY)
    return ApifyActorConnector(
        source=source,
        policy=source.policy or _live_policy(provider),
        token=_credential(provider),
        config=config,
    )


def taddy_for(source: Source) -> TaddyConnector:
    """Taddy needs BOTH secrets, so both are resolved before construction.

    Failing here names the missing half. Letting it through would surface as an
    auth error that reads like a bad key, and reissuing the key does not fix it.
    """
    provider = _provider(AcquisitionProvider.Kind.TADDY)
    return TaddyConnector(
        source=source,
        policy=source.policy or _live_policy(provider),
        api_key=_credential(provider, "api_key"),
        user_id=_credential(provider, "user_id"),
    )


def assemblyai_for(source: Source, *, model: str = DEFAULT_MODEL) -> AssemblyAIConnector:
    provider = _provider(AcquisitionProvider.Kind.ASSEMBLYAI)
    return AssemblyAIConnector(
        source=source,
        policy=source.policy or _live_policy(provider),
        api_key=_credential(provider),
        model=model,
    )
