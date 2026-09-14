from __future__ import annotations

from typing import Callable

from businessbuilder.access_broker.ports import SecretStorePort
from businessbuilder.agent_runtime.bootstrap import PostgresAgentRuntime
from businessbuilder.identity import AuthorizationPolicy

from .ports import OAuthProviderPort
from .service import ProviderConnectionService


def attach_provider_connections(
    app: PostgresAgentRuntime,
    *,
    secret_store: SecretStorePort,
    oauth_providers: dict[str, OAuthProviderPort],
    secret_locator_factory: Callable[[str, str, str], str] | None = None,
    clock=None,
) -> ProviderConnectionService:
    """Attach lifecycle handling to the existing Runtime/broker composition."""
    if app.broker is None:
        raise ValueError("provider connections require the existing secret/artifact broker")
    service = ProviderConnectionService(
        repository=app.runtime_repository,
        principal_authority=app.principal_authority,
        authorization=AuthorizationPolicy(app.identity_repository),
        broker=app.broker,
        secret_store=secret_store,
        providers=oauth_providers,
        audit=app.runtime.audit,
        clock=clock or app.runtime.clock,
        id_factory=app.runtime.id_factory,
        secret_locator_factory=secret_locator_factory,
    )
    app.broker.connection_tokens = service
    return service
