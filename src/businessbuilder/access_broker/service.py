from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Callable

from businessbuilder.agent_runtime.models import AgentJobEnvelope
from businessbuilder.agent_runtime.service import AgentRuntimeService
from businessbuilder.runtime import ArtifactRef
from businessbuilder.runtime import JobStatus
from businessbuilder.runtime.storage import RuntimeRepository

from .models import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    CapabilityGrant,
    ConnectionStatus,
    ExternalAccount,
    ExternalCredentialRef,
    JobSecretRef,
    ProviderActionResult,
    ProviderConnection,
    ProviderReceipt,
    ReceiptStatus,
    SignedArtifactAccess,
    stable_id,
)
from .ports import ArtifactStorePort, EphemeralArtifact, EphemeralSecret, ExternalProviderPort, SecretStorePort


CAPABILITY_SECRET_TYPES: dict[str, frozenset[str]] = {
    "communications.email": frozenset({"email.oauth", "email.api_token"}),
    "customer.intake": frozenset({"crm.oauth", "crm.api_token"}),
    "customer.quote": frozenset({"crm.oauth", "crm.api_token"}),
    "customer.service_history": frozenset({"crm.oauth", "crm.api_token"}),
    "company.policy": frozenset(),
}

CAPABILITY_ARTIFACT_CLASSES: dict[str, frozenset[ArtifactClassification]] = {
    "communications.email": frozenset({
        ArtifactClassification.EXTERNAL_ATTACHMENT,
        ArtifactClassification.BUSINESS_DOCUMENT,
        ArtifactClassification.BRAND_ASSET,
    }),
    "customer.intake": frozenset({
        ArtifactClassification.CUSTOMER_UPLOAD,
        ArtifactClassification.EXTERNAL_ATTACHMENT,
    }),
    "customer.quote": frozenset({
        ArtifactClassification.BUSINESS_DOCUMENT,
        ArtifactClassification.BRAND_ASSET,
    }),
    "customer.service_history": frozenset({ArtifactClassification.BUSINESS_DOCUMENT}),
    "company.policy": frozenset({ArtifactClassification.BUSINESS_DOCUMENT}),
}


class BrokerDenied(PermissionError):
    pass


class ProviderActionSuppressed(RuntimeError):
    def __init__(self, receipt: ProviderReceipt) -> None:
        super().__init__("provider action suppressed by durable idempotency receipt")
        self.receipt = receipt


class SimulatedBrokerCrash(RuntimeError):
    """Test-only crash after a provider accepted an action."""


class SecretArtifactBroker:
    """Capability-scoped material broker below the authoritative Runtime."""

    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        agent_runtime: AgentRuntimeService,
        secret_store: SecretStorePort,
        artifact_store: ArtifactStorePort,
        providers: dict[str, ExternalProviderPort],
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        maximum_artifact_bytes: int = 25 * 1024 * 1024,
        maximum_signed_seconds: int = 300,
        connection_tokens=None,
        outbound_safety=None,
    ) -> None:
        if maximum_artifact_bytes < 1 or maximum_signed_seconds not in range(1, 901):
            raise ValueError("broker limits are invalid")
        self.repository = repository
        self.agent_runtime = agent_runtime
        self.secret_store = secret_store
        self.artifact_store = artifact_store
        self.providers = providers
        self.clock = clock
        self.id_factory = id_factory
        self.maximum_artifact_bytes = maximum_artifact_bytes
        self.maximum_signed_seconds = maximum_signed_seconds
        self.connection_tokens = connection_tokens
        self.outbound_safety = outbound_safety

    def register_external_account(self, account: ExternalAccount, *, actor_id: str) -> None:
        self.repository.save_broker_record(
            "external_account", account.account_id, account.tenant_id, account.company_id, account
        )
        self._audit(account.tenant_id, account.company_id, actor_id, "broker.external_account.registered", "external_account", account.account_id, "registered", {"provider": account.provider, "status": account.status.value})

    def register_connection(self, connection: ProviderConnection, *, actor_id: str) -> None:
        account = self.repository.get_broker_record(
            "external_account", connection.tenant_id, connection.company_id, connection.account_id
        )
        if account is None or account.provider != connection.provider:
            raise BrokerDenied("provider connection does not match an external account")
        self.repository.save_broker_record(
            "provider_connection", connection.connection_id,
            connection.tenant_id, connection.company_id, connection,
        )
        self._audit(connection.tenant_id, connection.company_id, actor_id, "broker.connection.registered", "provider_connection", connection.connection_id, "registered", {"provider": connection.provider, "status": connection.status.value})

    def register_credential_ref(self, credential: ExternalCredentialRef, *, actor_id: str) -> None:
        connection = self.repository.get_broker_record(
            "provider_connection", credential.tenant_id, credential.company_id,
            credential.connection_id,
        )
        if connection is None or connection.provider != credential.provider:
            raise BrokerDenied("credential reference does not match a provider connection")
        self.repository.save_broker_record(
            "external_credential_ref", credential.secret_ref,
            credential.tenant_id, credential.company_id, credential,
        )
        self._audit(
            credential.tenant_id, credential.company_id, actor_id,
            "broker.credential.reference_registered", "external_credential_ref",
            credential.secret_ref, "opaque reference registered",
            {"provider": credential.provider, "secret_type": credential.secret_type, "rotated_at": credential.rotated_at},
        )

    def register_capability_grant(self, grant: CapabilityGrant, *, actor_id: str) -> None:
        connection = self.repository.get_broker_record(
            "provider_connection", grant.tenant_id, grant.company_id, grant.connection_id
        )
        if connection is None:
            raise BrokerDenied("capability grant connection is outside tenant/company scope")
        self.repository.save_broker_record(
            "capability_grant", grant.grant_id, grant.tenant_id, grant.company_id, grant
        )
        self._audit(grant.tenant_id, grant.company_id, actor_id, "broker.capability_grant.registered", "capability_grant", grant.grant_id, "least-privilege grant registered", {"agent_role": grant.agent_role, "capability": grant.scope.capability, "operations": sorted(grant.scope.operations), "secret_types": sorted(grant.scope.secret_types), "artifact_classifications": sorted(item.value for item in grant.artifact_classifications)})

    def register_artifact(
        self,
        *,
        tenant_id: str,
        company_id: str,
        artifact_id: str,
        content: bytes,
        content_type: str,
        classification: ArtifactClassification,
        provenance_ref: str,
        actor_id: str,
        expires_at: datetime | None = None,
        quarantined: bool = False,
    ) -> ArtifactRecord:
        if len(content) > self.maximum_artifact_bytes:
            raise ValueError("artifact exceeds broker size limit")
        content_hash = sha256(content).hexdigest()
        object_key = f"tenant/{tenant_id}/company/{company_id}/artifacts/{artifact_id}/{content_hash}"
        record = ArtifactRecord(
            artifact_id, tenant_id, company_id, object_key, content_hash,
            content_type, len(content), classification,
            ArtifactStatus.QUARANTINED if quarantined else ArtifactStatus.AVAILABLE,
            provenance_ref, self.clock(), expires_at,
        )
        self.artifact_store.put(
            object_key, content, content_type=content_type, content_sha256=content_hash
        )
        self.repository.save_broker_record("artifact", artifact_id, tenant_id, company_id, record)
        self._audit(tenant_id, company_id, actor_id, "broker.artifact.registered", "artifact", artifact_id, "artifact metadata and content hash registered", {"classification": classification.value, "content_type": content_type, "size_bytes": len(content), "content_sha256": content_hash, "status": record.status.value, "provenance_ref": provenance_ref})
        return record

    def revoke_connection(
        self,
        tenant_id: str,
        company_id: str,
        connection_id: str,
        *,
        status: ConnectionStatus = ConnectionStatus.REVOKED,
        reason: str,
        actor_id: str,
    ) -> ProviderConnection:
        if status is ConnectionStatus.ACTIVE:
            raise ValueError("revocation status cannot be active")
        connection = self.repository.get_broker_record(
            "provider_connection", tenant_id, company_id, connection_id
        )
        if connection is None:
            raise LookupError("provider connection not found in tenant/company scope")
        changed = replace(connection, status=status, updated_at=self.clock(), revoked_reason=reason[:200])
        self.repository.save_broker_record(
            "provider_connection", connection_id, tenant_id, company_id, changed
        )
        self._audit(tenant_id, company_id, actor_id, "broker.connection.revoked", "provider_connection", connection_id, reason[:200], {"provider": connection.provider, "status": status.value})
        return changed

    def resolve_secret(
        self,
        envelope: AgentJobEnvelope,
        reference: JobSecretRef,
        *,
        operation: str,
    ) -> EphemeralSecret:
        self._authorize_envelope(envelope)
        try:
            credential, _ = self._authorize_secret_record(envelope, reference, operation)
            material = self.secret_store.resolve(credential.secret_locator)
        except Exception as exc:
            self._audit_denial(envelope, "broker.secret.denied", "external_credential_ref", reference.secret_ref, type(exc).__name__)
            if isinstance(exc, BrokerDenied):
                raise
            raise BrokerDenied("secret reference resolution denied") from exc
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.secret.authorized", "external_credential_ref", reference.secret_ref, "ephemeral process-only resolution", {"job_id": envelope.job_id, "provider": reference.provider, "capability": envelope.capability, "operation": operation})
        return material

    def resolve_artifact(
        self,
        envelope: AgentJobEnvelope,
        artifact_ref: ArtifactRef,
        *,
        operation: str,
    ) -> EphemeralArtifact:
        self._authorize_envelope(envelope)
        try:
            artifact = self._authorize_artifact_record(envelope, artifact_ref, operation)
            material = self.artifact_store.read(
                artifact.object_key,
                maximum_bytes=min(self.maximum_artifact_bytes, artifact.size_bytes),
            )
            actual = material.use(lambda value: sha256(value).hexdigest())
            size = material.use(len)
            if actual != artifact.content_sha256 or size != artifact.size_bytes:
                material.close()
                raise BrokerDenied("artifact integrity validation failed")
        except Exception as exc:
            self._audit_denial(envelope, "broker.artifact.denied", "artifact", artifact_ref.id, type(exc).__name__)
            if isinstance(exc, BrokerDenied):
                raise
            raise BrokerDenied("artifact access denied") from exc
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.artifact.authorized", "artifact", artifact.artifact_id, "artifact scope and integrity verified", {"job_id": envelope.job_id, "capability": envelope.capability, "classification": artifact.classification.value, "content_sha256": artifact.content_sha256, "size_bytes": artifact.size_bytes})
        return material

    def issue_signed_download(
        self,
        envelope: AgentJobEnvelope,
        artifact_ref: ArtifactRef,
        *,
        operation: str,
        requested_seconds: int = 300,
    ) -> SignedArtifactAccess:
        with self.resolve_artifact(envelope, artifact_ref, operation=operation):
            artifact = self.repository.get_broker_record(
                "artifact", envelope.tenant_id, envelope.company_id, artifact_ref.id
            )
            assert artifact is not None
        remaining = int((envelope.expires_at - self.clock()).total_seconds())
        duration = min(requested_seconds, self.maximum_signed_seconds, remaining)
        if duration < 1:
            raise BrokerDenied("job expired before signed access could be issued")
        url = self.artifact_store.signed_download(
            artifact.object_key, expires_in_seconds=duration
        )
        expires_at = self.clock() + timedelta(seconds=duration)
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.artifact.signed_access_issued", "artifact", artifact.artifact_id, "short-lived download access issued", {"job_id": envelope.job_id, "capability": envelope.capability, "expires_at": expires_at, "content_sha256": artifact.content_sha256})
        return SignedArtifactAccess(
            artifact.artifact_id, artifact.content_sha256, artifact.content_type,
            artifact.size_bytes, expires_at, url,
        )

    def execute_provider_action(
        self,
        envelope: AgentJobEnvelope,
        *,
        operation: str,
        secret_ref: JobSecretRef,
        artifact_refs: tuple[ArtifactRef, ...] = (),
        crash_after_provider: bool = False,
    ) -> ProviderReceipt:
        self._authorize_envelope(envelope)
        credential, connection = self._authorize_secret_record(envelope, secret_ref, operation)
        communication_decision = None
        if self.outbound_safety is not None and operation.startswith("send_"):
            if not envelope.communication_ref:
                raise BrokerDenied("outbound communication requires an opaque policy request")
            communication_decision = self.outbound_safety.authorize(
                envelope, communication_ref=envelope.communication_ref,
                provider_connection_id=connection.connection_id, operation=operation,
            )
        if self.connection_tokens is not None:
            self.connection_tokens.refresh_for_job(
                envelope, secret_ref, operation=operation
            )
        credential, connection = self._authorize_secret_record(envelope, secret_ref, operation)
        for artifact_ref in artifact_refs:
            self._authorize_artifact_record(envelope, artifact_ref, operation)
        provider = self.providers.get(connection.provider)
        if provider is None or provider.provider != connection.provider:
            raise BrokerDenied("authorized provider adapter is unavailable")
        action_key = stable_id(
            "provider_action", envelope.tenant_id, envelope.company_id,
            envelope.job_id, connection.provider, operation, envelope.idempotency_key,
        )
        receipt = ProviderReceipt(
            stable_id("receipt", action_key), envelope.tenant_id, envelope.company_id,
            envelope.job_id, envelope.capability, connection.provider, operation,
            stable_id("provider_request", action_key), action_key,
            ReceiptStatus.IN_PROGRESS, self.clock(), "pending", None, False,
            connection_id=connection.connection_id,
            recipient_ref=communication_decision.recipient_id if communication_decision else None,
            communication_purpose=(
                self.outbound_safety._request(communication_decision).purpose.value
                if communication_decision else None
            ),
            policy_decision_id=communication_decision.decision_id if communication_decision else None,
            approval_reference=(
                self.outbound_safety._request(communication_decision).approval_ref
                if communication_decision else None
            ),
            suppression_result=communication_decision.suppression_result if communication_decision else None,
            content_policy_result=communication_decision.content_result if communication_decision else None,
            rate_limit_reservation_id=(
                communication_decision.rate_limit_reservation_id if communication_decision else None
            ),
            delivery_status="queued" if communication_decision else None,
            reconciliation_status="pending" if communication_decision else None,
        )
        claimed, execute = self.repository.claim_provider_receipt(receipt)
        if not execute:
            self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.provider_action.suppressed", "provider_receipt", claimed.receipt_id, "durable idempotency state prevented duplicate action", {"job_id": envelope.job_id, "provider": claimed.provider, "operation": claimed.operation, "status": claimed.status.value, "attempts": claimed.attempts})
            if claimed.status is ReceiptStatus.SUCCEEDED:
                if communication_decision:
                    self.outbound_safety.record_provider_result(envelope, communication_decision, claimed)
                return claimed
            raise ProviderActionSuppressed(claimed)
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.provider_action.attempted", "provider_receipt", claimed.receipt_id, "bounded external action claimed", {"job_id": envelope.job_id, "provider": connection.provider, "operation": operation, "provider_request_id": claimed.provider_request_id, "attempt": claimed.attempts})
        try:
            with ExitStack() as stack:
                secret = stack.enter_context(self.resolve_secret(envelope, secret_ref, operation=operation))
                artifacts = tuple(
                    stack.enter_context(self.resolve_artifact(envelope, ref, operation=operation))
                    for ref in artifact_refs
                )
                result = provider.execute(
                    operation=operation,
                    provider_request_id=claimed.provider_request_id,
                    idempotency_key=claimed.idempotency_key,
                    credential=secret,
                    artifacts=artifacts,
                )
                if crash_after_provider:
                    raise SimulatedBrokerCrash("simulated crash after provider acceptance")
        except SimulatedBrokerCrash:
            raise
        except Exception as exc:
            failed = replace(
                claimed, status=ReceiptStatus.FAILED,
                response_classification=type(exc).__name__, retryable=True,
                completed_at=self.clock(),
            )
            self.repository.complete_provider_receipt(failed)
            self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.provider_receipt.recorded", "provider_receipt", failed.receipt_id, "provider action failed safely", {"job_id": envelope.job_id, "provider": connection.provider, "operation": operation, "status": failed.status.value, "response_classification": failed.response_classification, "retryable": failed.retryable, "attempts": failed.attempts})
            raise
        completed = replace(
            claimed, status=result.status,
            response_classification=result.response_classification[:120],
            external_object_ref=result.external_object_ref,
            retryable=result.retryable, completed_at=self.clock(),
            delivery_status="accepted" if communication_decision and result.status is ReceiptStatus.SUCCEEDED else claimed.delivery_status,
            reconciliation_status="provider_receipt" if communication_decision else claimed.reconciliation_status,
        )
        self.repository.complete_provider_receipt(completed)
        if communication_decision:
            self.outbound_safety.record_provider_result(envelope, communication_decision, completed)
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.provider_receipt.recorded", "provider_receipt", completed.receipt_id, "non-sensitive provider receipt persisted", {"job_id": envelope.job_id, "provider": connection.provider, "operation": operation, "status": completed.status.value, "response_classification": completed.response_classification, "external_object_ref": completed.external_object_ref, "retryable": completed.retryable, "attempts": completed.attempts})
        return completed

    def reconcile_provider_action(
        self, envelope: AgentJobEnvelope, *, provider_name: str, operation: str
    ) -> ProviderReceipt:
        self._authorize_envelope(envelope)
        key = stable_id(
            "provider_action", envelope.tenant_id, envelope.company_id,
            envelope.job_id, provider_name, operation, envelope.idempotency_key,
        )
        receipt = self.repository.get_provider_receipt(
            envelope.tenant_id, envelope.company_id, provider_name, operation, key
        )
        if receipt is None or receipt.status is not ReceiptStatus.IN_PROGRESS:
            raise ProviderActionSuppressed(receipt) if receipt else LookupError("provider receipt not found")
        provider = self.providers.get(provider_name)
        if provider is None:
            raise BrokerDenied("provider reconciliation adapter unavailable")
        result = provider.reconcile(receipt.provider_request_id)
        if result is None:
            raise ProviderActionSuppressed(receipt)
        completed = replace(
            receipt, status=result.status,
            response_classification=result.response_classification[:120],
            external_object_ref=result.external_object_ref,
            retryable=result.retryable, completed_at=self.clock(),
        )
        self.repository.complete_provider_receipt(completed)
        if self.outbound_safety is not None and envelope.communication_ref:
            decision = self.repository.get_broker_record(
                "communication_policy_decision", envelope.tenant_id, envelope.company_id,
                stable_id("communication_decision", envelope.communication_ref, envelope.job_id),
            )
            if decision is not None:
                completed = replace(completed, delivery_status="accepted",
                                    reconciliation_status="provider_reconciled")
                self.repository.complete_provider_receipt(completed)
                self.outbound_safety.record_provider_result(envelope, decision, completed)
        self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", "broker.provider_receipt.reconciled", "provider_receipt", completed.receipt_id, "provider result recovered without repeating action", {"job_id": envelope.job_id, "provider": provider_name, "operation": operation, "status": completed.status.value})
        return completed

    def _authorize_envelope(self, envelope: AgentJobEnvelope) -> None:
        try:
            self.agent_runtime.revalidate(envelope)
            job = self.repository.get_job(
                envelope.tenant_id, envelope.company_id, envelope.job_id
            )
            if job is None or job.status not in {JobStatus.RUNNABLE, JobStatus.RUNNING}:
                raise BrokerDenied("Runtime job is not active for broker access")
        except Exception as exc:
            if isinstance(exc, BrokerDenied):
                raise
            raise BrokerDenied("Runtime job authorization is invalid") from exc

    def _authorize_secret_record(self, envelope: AgentJobEnvelope, reference: JobSecretRef, operation: str):
        if (
            reference.tenant_id != envelope.tenant_id
            or reference.company_id != envelope.company_id
            or reference.capability != envelope.capability
        ):
            raise BrokerDenied("secret reference scope does not match Runtime job")
        credential = self.repository.get_broker_record(
            "external_credential_ref", envelope.tenant_id, envelope.company_id,
            reference.secret_ref,
        )
        if credential is None or credential.provider != reference.provider:
            raise BrokerDenied("secret reference is not registered in Runtime scope")
        now = self.clock()
        if credential.status is not ConnectionStatus.ACTIVE or (credential.expires_at and credential.expires_at <= now):
            raise BrokerDenied("credential reference is not active")
        allowed_types = CAPABILITY_SECRET_TYPES.get(envelope.capability, frozenset())
        if credential.secret_type not in allowed_types:
            raise BrokerDenied("secret type is not permitted for capability")
        connection = self.repository.get_broker_record(
            "provider_connection", envelope.tenant_id, envelope.company_id,
            credential.connection_id,
        )
        account = self.repository.get_broker_record(
            "external_account", envelope.tenant_id, envelope.company_id,
            connection.account_id if connection else "missing",
        )
        if (
            connection is None or account is None
            or connection.status is not ConnectionStatus.ACTIVE
            or account.status is not ConnectionStatus.ACTIVE
            or (connection.expires_at and connection.expires_at <= now)
            or connection.provider != credential.provider
        ):
            raise BrokerDenied("provider connection is inactive")
        grants = self.repository.list_broker_records(
            "capability_grant", envelope.tenant_id, envelope.company_id
        )
        grant = next(
            (
                item for item in grants
                if item.connection_id == connection.connection_id
                and item.agent_role == envelope.agent_role
                and item.permits(
                    capability=envelope.capability, operation=operation,
                    secret_type=credential.secret_type, at=now,
                )
            ),
            None,
        )
        if grant is None:
            raise BrokerDenied("capability-scoped connection grant denied")
        return credential, connection

    def _authorize_artifact_record(self, envelope: AgentJobEnvelope, artifact_ref: ArtifactRef, operation: str) -> ArtifactRecord:
        if artifact_ref not in envelope.input_artifact_refs:
            raise BrokerDenied("artifact is not an immutable input of this Runtime job")
        artifact = self.repository.get_broker_record(
            "artifact", envelope.tenant_id, envelope.company_id, artifact_ref.id
        )
        now = self.clock()
        if (
            artifact is None or artifact.status is not ArtifactStatus.AVAILABLE
            or (artifact.expires_at and artifact.expires_at <= now)
            or artifact.size_bytes > self.maximum_artifact_bytes
        ):
            raise BrokerDenied("artifact is unavailable")
        allowed = CAPABILITY_ARTIFACT_CLASSES.get(envelope.capability, frozenset())
        if artifact.classification not in allowed:
            raise BrokerDenied("artifact classification is not permitted for capability")
        grants = self.repository.list_broker_records(
            "capability_grant", envelope.tenant_id, envelope.company_id
        )
        if not any(
            item.agent_role == envelope.agent_role
            and item.scope.capability == envelope.capability
            and operation in item.scope.operations
            and item.revoked_at is None
            and (item.expires_at is None or item.expires_at > now)
            and artifact.classification in item.artifact_classifications
            for item in grants
        ):
            raise BrokerDenied("artifact capability grant denied")
        return artifact

    def _audit_denial(self, envelope, action, target_type, target_id, reason) -> None:
        persisted = self.repository.get_agent_envelope(
            envelope.tenant_id, envelope.company_id, envelope.job_id
        )
        if persisted and persisted.envelope_digest == envelope.envelope_digest:
            self._audit(envelope.tenant_id, envelope.company_id, "secret_artifact_broker", action, target_type, target_id, reason, {"job_id": envelope.job_id, "capability": envelope.capability, "outcome": "denied"})

    def _audit(self, tenant_id, company_id, actor_id, action, target_type, target_id, reason, details) -> None:
        self.agent_runtime.runtime.audit.record(
            tenant_id=tenant_id,
            company_id=company_id,
            actor_type="system",
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            correlation_id=details.get("job_id", self.id_factory("correlation")),
            reason=reason,
            details=details,
            after={"target_id": target_id, "action": action},
        )
