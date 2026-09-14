from __future__ import annotations

from businessbuilder.agent_runtime.bootstrap import PostgresAgentRuntime
from businessbuilder.identity import AuthorizationPolicy

from .service import OutboundCommunicationSafety


def attach_outbound_communications(
    app: PostgresAgentRuntime,
    *,
    delivery_verifiers: dict[str, object],
    clock=None,
    limits=None,
) -> OutboundCommunicationSafety:
    if app.broker is None:
        raise ValueError("outbound communication safety requires the existing broker")
    service = OutboundCommunicationSafety(
        repository=app.runtime_repository,
        agent_runtime=app.service,
        principal_authority=app.principal_authority,
        authorization=AuthorizationPolicy(app.identity_repository),
        audit=app.runtime.audit,
        clock=clock or app.runtime.clock,
        id_factory=app.runtime.id_factory,
        delivery_verifiers=delivery_verifiers,
        limits=limits,
        company_brain=app.company_brain,
    )
    app.broker.outbound_safety = service
    return service
