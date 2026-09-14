from __future__ import annotations

from .service import LiveCanaryReadiness


def attach_live_canary_readiness(app, compliance, *, permit_signing_key: bytes,
                                 provider_adapters: dict[str, object], operator_verifier,
                                 founder_approval_verifier=None,
                                 runbook_ref: str = "docs/LIVE_COMMUNICATIONS_CANARY_RUNBOOKS.md"):
    return LiveCanaryReadiness(
        repository=app.runtime_repository,
        identity_repository=app.identity_repository,
        commercial_repository=app.commercial_repository,
        compliance=compliance,
        company_brain=app.company_brain,
        clock=app.runtime.clock,
        id_factory=app.runtime.id_factory,
        permit_signing_key=permit_signing_key,
        provider_adapters=provider_adapters,
        operator_verifier=operator_verifier,
        founder_approval_verifier=founder_approval_verifier,
        runbook_ref=runbook_ref,
    )
