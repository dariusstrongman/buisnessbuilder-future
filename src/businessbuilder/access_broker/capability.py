from __future__ import annotations

from businessbuilder.agent_runtime.capabilities import SAFE_AGENT_ACTIONS
from businessbuilder.runtime import (
    ArtifactRef,
    Capability,
    CapabilityFailure,
    CapabilityRequest,
    CapabilityResult,
    FailureKind,
    Money,
)

from .models import JobSecretRef, ReceiptStatus
from .service import BrokerDenied, ProviderActionSuppressed, SecretArtifactBroker


class BrokeredAgentCapability(Capability):
    """Runtime capability adapter whose provider access is exclusively brokered."""

    version = "v1"

    def __init__(self, name: str, broker: SecretArtifactBroker, *, estimated_minor: int = 0) -> None:
        self.name = name
        self.broker = broker
        self.estimated_minor = estimated_minor

    def validate_request(self, request: CapabilityRequest) -> None:
        role = request.inputs.get("agent_role")
        action = request.inputs.get("action")
        if role not in SAFE_AGENT_ACTIONS or action not in SAFE_AGENT_ACTIONS[role]:
            raise PermissionError("brokered adapter rejects unapproved role/action")
        refs = request.inputs.get("secret_refs")
        if not isinstance(refs, list) or len(refs) != 1:
            raise PermissionError("brokered provider action requires one opaque secret reference")
        JobSecretRef(**refs[0])

    def estimate(self, request: CapabilityRequest) -> Money:
        del request
        return Money("USD", self.estimated_minor)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        envelope = self.broker.repository.get_agent_envelope(
            request.tenant_id, request.company_id, request.job_id
        )
        if envelope is None:
            raise BrokerDenied("canonical Runtime envelope is missing")
        secret_ref = JobSecretRef(**request.inputs["secret_refs"][0])
        artifacts = tuple(
            ArtifactRef(**item) for item in request.inputs.get("input_artifact_refs", ())
        )
        try:
            receipt = self.broker.execute_provider_action(
                envelope,
                operation=str(request.inputs["action"]),
                secret_ref=secret_ref,
                artifact_refs=artifacts,
            )
        except ProviderActionSuppressed as exc:
            return CapabilityResult(
                exc.receipt.provider_request_id,
                "failed",
                (),
                Money("USD", 0),
                failure=CapabilityFailure(
                    "PROVIDER_ACTION_PENDING_RECONCILIATION",
                    "Provider action is already in progress and will not be repeated",
                    True,
                    FailureKind.RETRYABLE,
                ),
            )
        if receipt.status is not ReceiptStatus.SUCCEEDED:
            return CapabilityResult(
                receipt.provider_request_id,
                "failed",
                (),
                Money("USD", 0),
                failure=CapabilityFailure(
                    "PROVIDER_ACTION_NOT_ACCEPTED",
                    "Provider did not accept the bounded action",
                    receipt.retryable,
                    FailureKind.RETRYABLE if receipt.retryable else FailureKind.PERMANENT,
                ),
            )
        return CapabilityResult(
            receipt.external_object_ref or receipt.provider_request_id,
            "succeeded",
            (),
            Money("USD", self.estimated_minor),
            ("brokered_provider_action",),
        )

    def status(self, provider_ref: str) -> str:
        del provider_ref
        return "receipt_authoritative"

    def cancel(self, provider_ref: str) -> bool:
        del provider_ref
        return False

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        del provider_ref
        return None
