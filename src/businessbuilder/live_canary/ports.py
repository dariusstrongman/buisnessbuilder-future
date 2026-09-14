from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import ProviderDeliveryContract, ReconciliationState


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    state: ReconciliationState
    response_classification: str
    external_object_ref: str | None = None


class EmailDeliveryAdapter(Protocol):
    """Future live-provider boundary. Implementations must be disabled by construction here."""

    @property
    def contract(self) -> ProviderDeliveryContract: ...

    def reconcile(self, provider_request_id: str) -> ReconciliationResult: ...


class DeterministicEmailProviderEmulator:
    """No-network emulator used for readiness and reconciliation proofs."""

    def __init__(self, *, provider: str = "google-workspace-emulator") -> None:
        self._contract = ProviderDeliveryContract(
            provider=provider,
            oauth_scopes=frozenset({"mail.read", "mail.send"}),
            send_operation="reply_to_inbound",
            idempotency_mode="businessbuilder-receipt-plus-sent-message-reconciliation",
            rate_limit_model="provider-quota-plus-runtime-policy",
            callback_event_types=("delivery", "bounce", "complaint", "unsubscribe"),
            callback_verification_method="sandbox-hmac; future provider-specific verification required",
            callback_required_refs=("sandbox_verifier_ref",),
            token_refresh_behavior="broker-mediated-lease-and-rotation",
            error_classification=("retryable", "reconnect_required", "permanently_denied", "provider_unhealthy"),
            reconciliation_fallback="stable provider request ID lookup",
            sender_identity_requirements=("owned-domain", "spf", "dkim", "dmarc", "alignment"),
            network_delivery_enabled=False,
        )
        self._results: dict[str, ReconciliationResult] = {}

    @property
    def contract(self) -> ProviderDeliveryContract:
        return self._contract

    def set_result(self, provider_request_id: str, result: ReconciliationResult) -> None:
        self._results[provider_request_id] = result

    def reconcile(self, provider_request_id: str) -> ReconciliationResult:
        return self._results.get(provider_request_id, ReconciliationResult(
            ReconciliationState.UNKNOWN, "sandbox_result_unknown"))

    def send(self, *args, **kwargs):
        raise RuntimeError("network delivery is disabled in the canary-readiness build")


class DisabledGoogleWorkspaceAdapter:
    """Production-shaped Gmail boundary with no network execution capability."""

    def __init__(self) -> None:
        self._contract = ProviderDeliveryContract(
            provider="google-workspace",
            oauth_scopes=frozenset({
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            }),
            send_operation="users.messages.send",
            idempotency_mode="provider-receipt-message-fingerprint-and-sent-history-reconciliation",
            rate_limit_model="gmail per-user/project quota units plus Runtime limits",
            callback_event_types=("mailbox_history", "watch_expiration"),
            callback_verification_method="Cloud Pub/Sub OIDC JWT issuer/signature/audience/service-account binding",
            callback_required_refs=("pubsub_oidc_audience_ref", "allowed_service_account_ref"),
            token_refresh_behavior="broker-mediated refresh lease and credential-ref rotation",
            error_classification=("retryable", "reconnect_required", "permanently_denied", "provider_unhealthy"),
            reconciliation_fallback="Gmail history.list plus sent-message fingerprint lookup",
            sender_identity_requirements=("workspace mailbox", "owned domain", "SPF", "DKIM", "DMARC", "alignment"),
            network_delivery_enabled=False,
        )

    @property
    def contract(self) -> ProviderDeliveryContract:
        return self._contract

    def reconcile(self, provider_request_id: str) -> ReconciliationResult:
        del provider_request_id
        return ReconciliationResult(ReconciliationState.UNKNOWN,
                                    "live provider adapter disabled")

    def send(self, *args, **kwargs):
        raise RuntimeError("Google Workspace network delivery is not enabled")
