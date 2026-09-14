from __future__ import annotations

from businessbuilder.identity import AuthorizationPolicy

from .service import CommunicationsCompliance


def attach_communications_compliance(
    app,
    outbound_safety,
    *,
    unsubscribe_signing_key: bytes,
    jurisdiction_overlays=None,
    abuse_thresholds=None,
    operator_verifier=None,
    notification_adapter=None,
):
    kwargs = {}
    if abuse_thresholds is not None:
        kwargs["abuse_thresholds"] = abuse_thresholds
    service = CommunicationsCompliance(
        repository=app.runtime_repository,
        outbound_safety=outbound_safety,
        principal_authority=app.principal_authority,
        authorization=AuthorizationPolicy(app.identity_repository),
        audit=app.runtime.audit,
        clock=app.runtime.clock,
        id_factory=app.runtime.id_factory,
        unsubscribe_signing_key=unsubscribe_signing_key,
        jurisdiction_overlays=jurisdiction_overlays,
        operator_verifier=operator_verifier,
        notification_adapter=notification_adapter,
        **kwargs,
    )
    return service

