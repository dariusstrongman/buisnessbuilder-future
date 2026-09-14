from __future__ import annotations

import hmac
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Callable

from businessbuilder.access_broker.models import ConnectionStatus, stable_id
from businessbuilder.identity import (
    AuthenticatedPrincipal, AuthorizationContext, AuthorizationPolicy, Permission,
    PrincipalContextAuthority,
)
from businessbuilder.outbound_communications.models import (
    CommunicationPurpose, ConsentState, DeliveryStatus, DestinationType,
    PolicyOutcome, SuppressionState,
)
from businessbuilder.runtime.storage import RuntimeRepository

from .models import (
    AbuseSignal, AbuseSignalType, AbuseThresholds, AlertClass, AlertSeverity,
    ComplianceDecision, ConsentEvidence, ErasureRecord, JurisdictionContext,
    JurisdictionPolicy, KillSwitchRecord, KillSwitchScope, OperationalAlert,
    PIIClass, RetentionClass, RetentionRecord, RolloutPolicy, RolloutTier,
    UnsubscribeTokenRecord,
)


class ComplianceDenied(PermissionError):
    pass


POLICY_VERSION = "communications-compliance.us-federal.v1"
TERMS_VERSION = "sandbox-communications-terms.v1"
_PLATFORM_SCOPE = "platform_scope"


def us_federal_baseline() -> JurisdictionPolicy:
    """Narrow staging baseline. This is deliberately not represented as legal advice."""
    return JurisdictionPolicy(
        policy_id="us_federal_email",
        version=POLICY_VERSION,
        supported_jurisdictions=("US",),
        channel=DestinationType.EMAIL,
        purposes=tuple(CommunicationPurpose),
        allowed_consent=(
            ConsentState.EXPLICIT, ConsentState.TRANSACTIONAL_RELATIONSHIP,
            ConsentState.CUSTOMER_INITIATED, ConsentState.SERVICE_FOLLOWUP,
        ),
        retention_days=365,
        evidence_recordkeeping_days=2555,
        opt_out_required=True,
        disclosure_required=False,
        marketing_restricted=True,
        quiet_hours=None,
        current_consent_version_required=False,
        legally_reviewed=False,
    )


class CommunicationsCompliance:
    """Compliance gate below Runtime and outbound safety; never an orchestrator."""

    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        outbound_safety,
        principal_authority: PrincipalContextAuthority,
        authorization: AuthorizationPolicy,
        audit,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        unsubscribe_signing_key: bytes,
        jurisdiction_overlays: dict[str, JurisdictionPolicy] | None = None,
        abuse_thresholds: AbuseThresholds = AbuseThresholds(),
        operator_verifier: Callable[[object], str] | None = None,
        sandbox_providers: frozenset[str] = frozenset({"sandbox-email", "test-email"}),
        notification_adapter=None,
    ) -> None:
        if len(unsubscribe_signing_key) < 32:
            raise ValueError("unsubscribe signing key must be at least 32 bytes")
        self.repository = repository
        self.outbound_safety = outbound_safety
        self.principal_authority = principal_authority
        self.authorization = authorization
        self.audit = audit
        self.clock = clock
        self.id_factory = id_factory
        self._unsubscribe_key = unsubscribe_signing_key
        self.policies = {"US": us_federal_baseline(), **(jurisdiction_overlays or {})}
        self.abuse_thresholds = abuse_thresholds
        self.operator_verifier = operator_verifier
        self.sandbox_providers = sandbox_providers
        self.notification_adapter = notification_adapter
        self.live_send_enabled = False  # hard server-side build invariant for this branch
        self.canary_readiness = None
        outbound_safety.compliance = self

    def configure_jurisdiction(
        self, principal: AuthenticatedPrincipal, *, tenant_id: str, company_id: str,
        tenant_jurisdiction: str, company_jurisdiction: str,
        recipient_id: str | None = None, recipient_jurisdiction: str | None = None,
        source_ref: str,
    ) -> JurisdictionContext:
        verified = self._manage(principal, tenant_id, company_id)
        context = JurisdictionContext(
            tenant_id, company_id, tenant_jurisdiction, company_jurisdiction,
            recipient_id, recipient_jurisdiction, self.clock(), source_ref,
        )
        record_id = self._jurisdiction_id(tenant_id, company_id, recipient_id)
        self.repository.save_broker_record(
            "communication_compliance_jurisdiction", record_id, tenant_id, company_id, context,
        )
        self._audit(tenant_id, company_id, verified.user_id, "compliance.jurisdiction.configured",
                    "jurisdiction", record_id, "jurisdiction context explicitly configured",
                    {"source_ref": source_ref})
        return context

    def record_canonical_consent(self, recipient, *, source_event_ref: str) -> ConsentEvidence:
        event = next((e for e in self.repository.list_events(recipient.tenant_id, recipient.company_id)
                      if e["event_id"] == source_event_ref), None)
        if event is None:
            raise ComplianceDenied("consent evidence source is not a canonical event")
        now = self.clock()
        evidence = ConsentEvidence(
            stable_id("consent_evidence", recipient.recipient_id, source_event_ref),
            recipient.tenant_id, recipient.company_id, recipient.recipient_id, None,
            DestinationType.EMAIL, recipient.consent_state, "canonical_event", now,
            source_event_ref, source_event_ref, POLICY_VERSION, None,
            now + timedelta(days=365),
        )
        self.repository.save_broker_record(
            "communication_compliance_consent", evidence.consent_evidence_id,
            evidence.tenant_id, evidence.company_id, evidence,
        )
        self._classify(
            evidence.tenant_id, evidence.company_id, "consent_evidence",
            evidence.consent_evidence_id, (PIIClass.CONSENT_EVIDENCE,),
            RetentionClass.COMPLIANCE_EVIDENCE, 2555, hold=True,
        )
        self._audit(evidence.tenant_id, evidence.company_id, "canonical_event",
                    "compliance.consent.captured", "consent_evidence",
                    evidence.consent_evidence_id, "attributable canonical consent evidence",
                    {"recipient_id": evidence.recipient_id, "source_event_ref": source_event_ref,
                     "consent_basis": evidence.consent_basis.value,
                     "policy_version": evidence.policy_version})
        return evidence

    def capture_consent(
        self, principal: AuthenticatedPrincipal, *, tenant_id: str, company_id: str,
        recipient_id: str, purpose: CommunicationPurpose, consent_basis: ConsentState,
        source: str, evidence_ref: str, terms_version: str | None = TERMS_VERSION,
        expires_at: datetime | None = None, supersedes_id: str | None = None,
    ) -> ConsentEvidence:
        verified = self._manage(principal, tenant_id, company_id)
        if consent_basis in {ConsentState.UNKNOWN, ConsentState.WITHDRAWN, ConsentState.PROHIBITED}:
            raise ComplianceDenied("active consent evidence requires an affirmative basis")
        self.outbound_safety._recipient(tenant_id, company_id, recipient_id)
        now = self.clock()
        evidence = ConsentEvidence(
            self.id_factory("consent"), tenant_id, company_id, recipient_id, purpose,
            DestinationType.EMAIL, consent_basis, source, now, verified.user_id,
            evidence_ref, POLICY_VERSION, terms_version,
            expires_at or now + timedelta(days=365), supersedes_id=supersedes_id,
        )
        with self.repository.transaction():
            if supersedes_id:
                old = self.repository.get_broker_record(
                    "communication_compliance_consent", tenant_id, company_id, supersedes_id)
                if old is None or old.recipient_id != recipient_id:
                    raise ComplianceDenied("superseded consent is outside recipient scope")
                self.repository.save_broker_record(
                    "communication_compliance_consent", old.consent_evidence_id,
                    tenant_id, company_id, replace(old, superseded_by_id=evidence.consent_evidence_id),
                )
            self.repository.save_broker_record(
                "communication_compliance_consent", evidence.consent_evidence_id,
                tenant_id, company_id, evidence,
            )
            self._classify(tenant_id, company_id, "consent_evidence",
                           evidence.consent_evidence_id, (PIIClass.CONSENT_EVIDENCE,),
                           RetentionClass.COMPLIANCE_EVIDENCE, 2555, hold=True)
        self._audit(tenant_id, company_id, verified.user_id, "compliance.consent.captured",
                    "consent_evidence", evidence.consent_evidence_id,
                    "authorized consent evidence captured",
                    {"recipient_id": recipient_id, "purpose": purpose.value,
                     "policy_version": POLICY_VERSION, "terms_version": terms_version})
        return evidence

    def validate_reenable(self, recipient) -> None:
        context = self._jurisdiction(recipient.tenant_id, recipient.company_id,
                                     recipient.recipient_id)
        if context is None or not context.recipient_jurisdiction \
                or context.recipient_jurisdiction not in self.policies:
            raise ComplianceDenied("unsupported jurisdiction")
        if recipient.suppression_reason in {"legal_compliance_block", "complaint"}:
            raise ComplianceDenied("policy does not permit recipient re-enable")

    def record_authorized_reenable(self, recipient, *, actor_id: str,
                                   consent_provenance: str) -> ConsentEvidence:
        now = self.clock()
        evidence = ConsentEvidence(
            self.id_factory("consent"), recipient.tenant_id, recipient.company_id,
            recipient.recipient_id, None, DestinationType.EMAIL, ConsentState.EXPLICIT,
            "authorized_reenable", now, actor_id, consent_provenance,
            POLICY_VERSION, TERMS_VERSION, now + timedelta(days=365),
        )
        self.repository.save_broker_record(
            "communication_compliance_consent", evidence.consent_evidence_id,
            evidence.tenant_id, evidence.company_id, evidence)
        self._classify(evidence.tenant_id, evidence.company_id, "consent_evidence",
                       evidence.consent_evidence_id, (PIIClass.CONSENT_EVIDENCE,),
                       RetentionClass.COMPLIANCE_EVIDENCE, 2555, hold=True)
        return evidence

    def issue_unsubscribe_token(self, *, tenant_id: str, company_id: str,
                                recipient_id: str,
                                lifetime: timedelta = timedelta(days=30)) -> str:
        self.outbound_safety._recipient(tenant_id, company_id, recipient_id)
        if lifetime <= timedelta(0) or lifetime > timedelta(days=90):
            raise ValueError("unsubscribe token lifetime is invalid")
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(self._unsubscribe_key, nonce.encode(), sha256).hexdigest()
        token = f"{nonce}.{signature}"
        digest = sha256(token.encode()).hexdigest()
        record_id = stable_id("unsubscribe_token", digest)
        now = self.clock()
        record = UnsubscribeTokenRecord(
            record_id, digest, tenant_id, company_id, recipient_id,
            now, now + lifetime,
        )
        self.repository.save_broker_record(
            "communication_compliance_unsubscribe_token", record_id,
            tenant_id, company_id, record,
        )
        return token

    def unsubscribe(self, token: str) -> bool:
        record = self._verify_unsubscribe_token(token)
        if record is None:
            return False
        if record.consumed_at is not None:
            return True
        now = self.clock()
        with self.repository.transaction():
            current = self.repository.get_broker_record_by_id(
                "communication_compliance_unsubscribe_token", record.token_record_id)
            if current is None or current.expires_at <= now:
                return False
            if current.consumed_at is not None:
                return True
            self.outbound_safety._suppress(
                record.tenant_id, record.company_id, record.recipient_id,
                event_id=f"unsubscribe_{record.token_record_id}", reason="recipient_opt_out",
                actor_id="recipient_unsubscribe", withdrawn=True,
            )
            self.repository.save_broker_record(
                "communication_compliance_unsubscribe_token", record.token_record_id,
                record.tenant_id, record.company_id, replace(current, consumed_at=now),
            )
            self._withdraw_consent(record.tenant_id, record.company_id, record.recipient_id, now)
        self.record_abuse_signal(record.tenant_id, record.company_id, AbuseSignalType.OPT_OUT,
                                 record.token_record_id)
        self._audit(record.tenant_id, record.company_id, "recipient",
                    "compliance.unsubscribe.completed", "recipient", record.recipient_id,
                    "unsubscribe token accepted", {"token_record_id": record.token_record_id})
        return True

    def evaluate_admission(self, communication) -> ComplianceDecision:
        recipient = self.outbound_safety._recipient(
            communication.tenant_id, communication.company_id, communication.recipient_id)
        return self._evaluate(communication, recipient, job_id=None, stage="admission")

    def evaluate_execution(self, envelope, communication, recipient) -> ComplianceDecision:
        return self._evaluate(communication, recipient, job_id=envelope.job_id, stage="execution")

    def _evaluate(self, communication, recipient, *, job_id, stage) -> ComplianceDecision:
        now = self.clock()
        reason = "compliance_allowed"
        outcome = PolicyOutcome.ALLOWED
        context = self._jurisdiction(communication.tenant_id, communication.company_id,
                                     communication.recipient_id)
        policy = None
        rollout = self._rollout(communication.tenant_id, communication.company_id)
        connection = self.repository.get_broker_record(
            "provider_connection", communication.tenant_id, communication.company_id,
            communication.provider_connection_id)
        try:
            if context is None:
                raise ComplianceDenied("unsupported jurisdiction")
            jurisdictions = (context.tenant_jurisdiction, context.company_jurisdiction,
                             context.recipient_jurisdiction)
            if any(not item or item not in self.policies for item in jurisdictions):
                raise ComplianceDenied("unsupported jurisdiction")
            policy = self.policies[context.recipient_jurisdiction]
            if DestinationType.EMAIL is not policy.channel or communication.purpose not in policy.purposes:
                raise ComplianceDenied("jurisdiction policy does not allow purpose")
            if recipient.consent_state not in policy.allowed_consent:
                raise ComplianceDenied("consent basis is not allowed by current policy")
            evidence = self._active_consent(communication, now)
            if evidence is None:
                raise ComplianceDenied("active attributable consent evidence is required")
            if policy.current_consent_version_required and evidence.policy_version != policy.version:
                raise ComplianceDenied("consent requires migration or reconsent")
            if self._killed(communication):
                raise ComplianceDenied("outbound kill switch is engaged")
            if rollout.tier is not RolloutTier.SANDBOX:
                raise ComplianceDenied("communications rollout is not sandbox")
            if self.live_send_enabled:
                raise ComplianceDenied("live-send build invariant violated")
            if connection is None or connection.provider not in self.sandbox_providers:
                raise ComplianceDenied("live or unapproved provider is disabled")
            destination_domain = recipient.normalized_destination.rsplit("@", 1)[-1]
            if (communication.tenant_id not in rollout.approved_tenants
                    or communication.company_id not in rollout.approved_companies
                    or connection.provider not in rollout.approved_providers
                    or communication.purpose not in rollout.approved_purposes
                    or destination_domain not in rollout.approved_recipient_domains):
                raise ComplianceDenied("rollout allowlist denied communication")
            if rollout.founder_approval_required and not communication.approval_ref:
                raise ComplianceDenied("rollout requires founder approval")
            if policy.quiet_hours and self._in_quiet_hours(now.hour, policy.quiet_hours):
                raise ComplianceDenied("jurisdiction quiet hours deny communication")
            if communication.purpose in {CommunicationPurpose.MARKETING,
                                          CommunicationPurpose.RE_ENGAGEMENT}:
                if evidence.purpose is not communication.purpose:
                    raise ComplianceDenied("purpose-specific marketing consent is required")
            if self.canary_readiness is not None:
                self.canary_readiness.validate_simulated_send(
                    communication, recipient, stage=stage)
        except (ComplianceDenied, PermissionError) as exc:
            outcome = PolicyOutcome.DENIED
            reason = self._reason(exc)
        decision = ComplianceDecision(
            stable_id("compliance_decision", communication.communication_id, stage, job_id or "none"),
            communication.tenant_id, communication.company_id, communication.communication_id,
            job_id, communication.recipient_id, communication.purpose,
            context.tenant_jurisdiction if context else "unknown",
            context.company_jurisdiction if context else "unknown",
            context.recipient_jurisdiction if context and context.recipient_jurisdiction else "unknown",
            policy.version if policy else "unknown", recipient.consent_state,
            recipient.suppression_state.value,
            RetentionClass.DELIVERY_OPERATIONS,
            "present" if communication.approval_ref else "not_required_or_missing",
            rollout.tier, "engaged" if self._killed(communication) else "clear",
            outcome, reason, stage, now,
        )
        self.repository.save_broker_record(
            "communication_compliance_decision", decision.compliance_decision_id,
            decision.tenant_id, decision.company_id, decision,
        )
        self._audit(decision.tenant_id, decision.company_id, "compliance_policy",
                    "compliance.policy.allowed" if outcome is PolicyOutcome.ALLOWED else "compliance.policy.denied",
                    "communication", decision.communication_id, reason,
                    {"recipient_id": decision.recipient_id, "purpose": decision.purpose.value,
                     "policy_version": decision.policy_version, "rollout_tier": decision.rollout_tier.value,
                     "stage": stage, "job_id": job_id})
        if outcome is PolicyOutcome.DENIED:
            self.record_abuse_signal(decision.tenant_id, decision.company_id,
                                     AbuseSignalType.DENIED_SEND, decision.compliance_decision_id)
            raise ComplianceDenied(reason)
        self._classify(decision.tenant_id, decision.company_id, "communication_decision",
                       decision.compliance_decision_id,
                       (PIIClass.CONSENT_EVIDENCE, PIIClass.AUDIT_METADATA),
                       RetentionClass.COMPLIANCE_EVIDENCE,
                       policy.evidence_recordkeeping_days if policy else 2555, hold=True)
        return decision

    def set_company_kill_switch(self, principal: AuthenticatedPrincipal, *, tenant_id: str,
                                company_id: str, engaged: bool,
                                reason_code: str) -> KillSwitchRecord:
        verified = self._manage(principal, tenant_id, company_id)
        return self._set_kill(KillSwitchScope.COMPANY, company_id, tenant_id, company_id,
                              engaged, reason_code, verified.user_id)

    def set_platform_kill_switch(self, operator_context: object, *, scope: KillSwitchScope,
                                 scope_id: str, tenant_id: str = _PLATFORM_SCOPE,
                                 company_id: str = _PLATFORM_SCOPE, engaged: bool,
                                 reason_code: str) -> KillSwitchRecord:
        if self.operator_verifier is None:
            raise ComplianceDenied("platform operator verifier is unavailable")
        actor_id = self.operator_verifier(operator_context)
        if not actor_id:
            raise ComplianceDenied("trusted platform operator context required")
        if scope is KillSwitchScope.COMPANY:
            raise ComplianceDenied("company controls require a customer principal")
        return self._set_kill(scope, scope_id, tenant_id, company_id,
                              engaged, reason_code, actor_id)

    def _set_kill(self, scope, scope_id, tenant_id, company_id, engaged, reason, actor):
        record_id = self._kill_id(scope, scope_id)
        record = KillSwitchRecord(record_id, scope, scope_id, tenant_id, company_id,
                                  engaged, reason, actor, self.clock())
        self.repository.save_broker_record("communication_compliance_kill_switch", record_id,
                                           tenant_id, company_id, record)
        action = "engaged" if engaged else "disengaged"
        self._audit(tenant_id, company_id, actor, f"compliance.kill_switch.{action}",
                    "kill_switch", record_id, reason,
                    {"scope": scope.value, "scope_id": scope_id, "engaged": engaged})
        if engaged:
            self._create_alert(tenant_id, company_id, AlertClass.KILL_SWITCH_ENGAGED,
                               AlertSeverity.SUSPEND, reason, record_id)
        return record

    def set_rollout(self, principal: AuthenticatedPrincipal, *, tenant_id: str,
                    company_id: str, tier: RolloutTier) -> RolloutPolicy:
        verified = self._manage(principal, tenant_id, company_id)
        if tier not in {RolloutTier.DISABLED, RolloutTier.SANDBOX}:
            raise ComplianceDenied("live rollout tiers are compile-time disabled")
        current = self._rollout(tenant_id, company_id)
        changed = replace(current, tier=tier, changed_by=verified.user_id,
                          changed_at=self.clock())
        self.repository.save_broker_record("communication_compliance_rollout",
                                           changed.rollout_id, tenant_id, company_id, changed)
        self._audit(tenant_id, company_id, verified.user_id,
                    "compliance.rollout.changed", "rollout", changed.rollout_id,
                    "sandbox-safe rollout transition",
                    {"tier": tier.value, "live_send_enabled": False})
        return changed

    def handle_verified_delivery(self, delivery, event) -> None:
        self._audit(delivery.tenant_id, delivery.company_id, event.provider,
                    "compliance.callback.verified", "delivery", delivery.delivery_id,
                    "provider callback authenticity verified",
                    {"status": event.status.value, "provider_event_id": event.event_id})
        if event.status is DeliveryStatus.BOUNCED:
            self.record_abuse_signal(delivery.tenant_id, delivery.company_id,
                                     AbuseSignalType.HARD_BOUNCE, event.event_id)
        elif event.status is DeliveryStatus.COMPLAINED:
            self.record_abuse_signal(delivery.tenant_id, delivery.company_id,
                                     AbuseSignalType.COMPLAINT, event.event_id)

    def record_delivery_retention(self, delivery, receipt) -> None:
        self._classify(delivery.tenant_id, delivery.company_id, "delivery",
                       delivery.delivery_id,
                       (PIIClass.DELIVERY_METADATA, PIIClass.PROVIDER_IDENTIFIER),
                       RetentionClass.DELIVERY_OPERATIONS, 365, hold=False)
        self._classify(receipt.tenant_id, receipt.company_id, "provider_receipt",
                       receipt.receipt_id,
                       (PIIClass.DELIVERY_METADATA, PIIClass.PROVIDER_IDENTIFIER),
                       RetentionClass.PROVIDER_RECEIPT, 730, hold=True)

    def callback_verification_failed(self, provider: str) -> None:
        self._create_alert(_PLATFORM_SCOPE, _PLATFORM_SCOPE,
                           AlertClass.CALLBACK_VERIFICATION_FAILURE,
                           AlertSeverity.WARNING, "callback_authenticity_failed",
                           stable_id("callback_failure", provider, self.clock().isoformat()))

    def record_abuse_signal(self, tenant_id: str, company_id: str,
                            signal_type: AbuseSignalType, source_ref: str) -> AbuseSignal:
        signal_id = stable_id("abuse_signal", tenant_id, company_id,
                              signal_type.value, source_ref)
        prior = self.repository.get_broker_record(
            "communication_compliance_abuse_signal", tenant_id, company_id, signal_id)
        if prior:
            return prior
        signal = AbuseSignal(signal_id, tenant_id, company_id, signal_type,
                             source_ref, self.clock())
        self.repository.save_broker_record("communication_compliance_abuse_signal", signal_id,
                                           tenant_id, company_id, signal)
        cutoff = self.clock() - timedelta(seconds=self.abuse_thresholds.window_seconds)
        count = sum(1 for item in self.repository.list_broker_records(
            "communication_compliance_abuse_signal", tenant_id, company_id)
                    if item.signal_type is signal_type and item.occurred_at >= cutoff)
        severity = None
        if count >= self.abuse_thresholds.emergency:
            severity = AlertSeverity.EMERGENCY
        elif count >= self.abuse_thresholds.suspend:
            severity = AlertSeverity.SUSPEND
        elif count >= self.abuse_thresholds.throttle:
            severity = AlertSeverity.THROTTLE
        elif count >= self.abuse_thresholds.warning:
            severity = AlertSeverity.WARNING
        if severity:
            alert_class = {
                AbuseSignalType.COMPLAINT: AlertClass.COMPLAINT_SPIKE,
                AbuseSignalType.HARD_BOUNCE: AlertClass.BOUNCE_SPIKE,
                AbuseSignalType.CALLBACK_VERIFICATION_FAILURE: AlertClass.CALLBACK_VERIFICATION_FAILURE,
                AbuseSignalType.PROVIDER_AUTH_FAILURE: AlertClass.PROVIDER_AUTH_FAILURE,
            }.get(signal_type, AlertClass.SEND_RATE_ANOMALY)
            self._create_alert(tenant_id, company_id, alert_class, severity,
                               f"{signal_type.value}_threshold_{severity.value}", signal_id)
            if severity in {AlertSeverity.SUSPEND, AlertSeverity.EMERGENCY}:
                self._set_kill(KillSwitchScope.COMPANY, company_id, tenant_id, company_id,
                               True, f"abuse_{signal_type.value}_{severity.value}",
                               "abuse_monitor")
        return signal

    def request_erasure(self, principal: AuthenticatedPrincipal, *, tenant_id: str,
                        company_id: str, recipient_id: str,
                        reason: str) -> ErasureRecord:
        verified = self._manage(principal, tenant_id, company_id)
        recipient = self.outbound_safety._recipient(tenant_id, company_id, recipient_id)
        retention = self._retention_for(tenant_id, company_id, "recipient", recipient_id)
        now = self.clock()
        blocked = bool(retention and retention.compliance_hold)
        erasure_id = stable_id("erasure", tenant_id, company_id, recipient_id, reason)
        current = self.repository.get_broker_record("communication_compliance_erasure",
                                                    tenant_id, company_id, erasure_id)
        if current:
            return current
        if not blocked:
            digest = recipient.destination_digest[:24]
            tombstone = replace(
                recipient,
                normalized_destination=f"deleted+{digest}@redacted.example.test",
                consent_state=ConsentState.WITHDRAWN,
                consent_provenance="erasure_tombstone",
                suppression_state=SuppressionState.SUPPRESSED,
                suppression_reason="customer_request",
                opt_out_at=recipient.opt_out_at or now,
                risk_flags=(), updated_at=now,
            )
            self.repository.save_broker_record("communication_recipient", recipient_id,
                                               tenant_id, company_id, tombstone)
            if retention:
                self.repository.save_broker_record(
                    "communication_compliance_retention", retention.retention_record_id,
                    tenant_id, company_id,
                    replace(retention, deletion_eligible=True, deletion_reason=reason,
                            deleted_at=now, pseudonymized=True),
                )
        result = ErasureRecord(erasure_id, tenant_id, company_id, recipient_id,
                               verified.user_id, reason, now, now,
                               not blocked, True, blocked)
        self.repository.save_broker_record("communication_compliance_erasure", erasure_id,
                                           tenant_id, company_id, result)
        self._audit(tenant_id, company_id, verified.user_id,
                    "compliance.erasure.blocked" if blocked else "compliance.erasure.completed",
                    "recipient", recipient_id,
                    "compliance hold preserved" if blocked else "operational PII pseudonymized",
                    {"erasure_id": erasure_id, "compliance_evidence_preserved": True})
        return result

    def set_retention_hold(self, operator_context: object, *, tenant_id: str,
                           company_id: str, subject_type: str, subject_id: str,
                           hold: bool) -> RetentionRecord:
        if self.operator_verifier is None or not self.operator_verifier(operator_context):
            raise ComplianceDenied("trusted platform operator context required")
        record = self._retention_for(tenant_id, company_id, subject_type, subject_id)
        if record is None:
            raise LookupError("retention record not found")
        changed = replace(record, compliance_hold=hold)
        self.repository.save_broker_record("communication_compliance_retention",
                                           record.retention_record_id,
                                           tenant_id, company_id, changed)
        return changed

    def customer_status(self, principal, *, tenant_id: str, company_id: str) -> dict:
        self._view(principal, tenant_id, company_id)
        rollout = self._rollout(tenant_id, company_id)
        killed = self._switch(KillSwitchScope.COMPANY, company_id)
        recipients = self.repository.list_broker_records("communication_recipient", tenant_id, company_id)
        connections = self.repository.list_broker_records("provider_connection", tenant_id, company_id)
        return {
            "connection": "connected" if any(c.status is ConnectionStatus.ACTIVE for c in connections)
                          else "needs_reconnect",
            "mode": "sandbox_only",
            "sending": "paused" if rollout.tier is RolloutTier.DISABLED or (killed and killed.engaged) else "sandbox",
            "suppressed_recipients_present": any(r.suppression_state is SuppressionState.SUPPRESSED for r in recipients),
            "action_required": bool(killed and killed.engaged),
            "live_send_enabled": False,
        }

    def list_alerts(self, principal, *, tenant_id: str, company_id: str):
        self._view(principal, tenant_id, company_id)
        return self.repository.list_broker_records("communication_compliance_alert", tenant_id, company_id)

    def list_consent(self, principal, *, tenant_id: str, company_id: str, recipient_id: str):
        self._view(principal, tenant_id, company_id)
        self.outbound_safety._recipient(tenant_id, company_id, recipient_id)
        return tuple(item for item in self.repository.list_broker_records(
            "communication_compliance_consent", tenant_id, company_id)
                     if item.recipient_id == recipient_id)

    def operator_summary(self, operator_context: object, *, tenant_id: str,
                         company_id: str) -> dict:
        if self.operator_verifier is None or not self.operator_verifier(operator_context):
            raise ComplianceDenied("trusted platform operator context required")
        return {
            "tenant_id": tenant_id, "company_id": company_id,
            "alerts": len(self.repository.list_broker_records("communication_compliance_alert", tenant_id, company_id)),
            "abuse_signals": len(self.repository.list_broker_records("communication_compliance_abuse_signal", tenant_id, company_id)),
            "decisions": len(self.repository.list_broker_records("communication_compliance_decision", tenant_id, company_id)),
            "live_send_enabled": False,
        }

    def enforce_retention(self, operator_context: object, *, tenant_id: str,
                          company_id: str) -> dict[str, int]:
        if self.operator_verifier is None or not self.operator_verifier(operator_context):
            raise ComplianceDenied("trusted platform operator context required")
        now = self.clock()
        expired = preserved = pseudonymized = 0
        for record in self.repository.list_broker_records(
                "communication_compliance_retention", tenant_id, company_id):
            if record.deleted_at is not None or record.expires_at > now:
                continue
            expired += 1
            if record.compliance_hold or record.retention_class in {
                    RetentionClass.COMPLIANCE_EVIDENCE, RetentionClass.AUDIT_REQUIRED}:
                preserved += 1
                continue
            if record.subject_type == "recipient":
                recipient = self.repository.get_broker_record(
                    "communication_recipient", tenant_id, company_id, record.subject_id)
                if recipient and not recipient.normalized_destination.endswith("@redacted.example.test"):
                    digest = recipient.destination_digest[:24]
                    self.repository.save_broker_record(
                        "communication_recipient", recipient.recipient_id, tenant_id, company_id,
                        replace(recipient,
                                normalized_destination=f"deleted+{digest}@redacted.example.test",
                                consent_state=ConsentState.WITHDRAWN,
                                consent_provenance="retention_expiration",
                                suppression_state=SuppressionState.SUPPRESSED,
                                suppression_reason="retention_expiration",
                                opt_out_at=recipient.opt_out_at or now,
                                risk_flags=(), updated_at=now))
                    pseudonymized += 1
            self.repository.save_broker_record(
                "communication_compliance_retention", record.retention_record_id,
                tenant_id, company_id,
                replace(record, deletion_eligible=True,
                        deletion_reason="retention_expired", deleted_at=now,
                        pseudonymized=record.subject_type == "recipient"))
            self._audit(tenant_id, company_id, "retention_enforcer",
                        "compliance.retention.enforced", record.subject_type,
                        record.subject_id, "retention_expired",
                        {"retention_class": record.retention_class.value,
                         "pseudonymized": record.subject_type == "recipient"})
        return {"expired": expired, "preserved": preserved,
                "pseudonymized": pseudonymized}

    def _active_consent(self, communication, at):
        candidates = self.repository.list_broker_records(
            "communication_compliance_consent", communication.tenant_id, communication.company_id)
        return next((item for item in reversed(candidates)
                     if item.recipient_id == communication.recipient_id
                     and item.channel is DestinationType.EMAIL
                     and item.purpose in {None, communication.purpose}
                     and item.active_at(at)), None)

    def _withdraw_consent(self, tenant_id, company_id, recipient_id, at):
        for item in self.repository.list_broker_records("communication_compliance_consent",
                                                        tenant_id, company_id):
            if item.recipient_id == recipient_id and item.withdrawn_at is None:
                self.repository.save_broker_record(
                    "communication_compliance_consent", item.consent_evidence_id,
                    tenant_id, company_id, replace(item, withdrawn_at=at))

    def _verify_unsubscribe_token(self, token):
        if not isinstance(token, str) or len(token) > 200 or token.count(".") != 1:
            return None
        nonce, signature = token.split(".")
        expected = hmac.new(self._unsubscribe_key, nonce.encode(), sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        digest = sha256(token.encode()).hexdigest()
        record = self.repository.get_broker_record_by_id(
            "communication_compliance_unsubscribe_token",
            stable_id("unsubscribe_token", digest))
        if record is None or not hmac.compare_digest(record.token_digest, digest):
            return None
        if record.expires_at <= self.clock():
            return None
        return record

    def _classify(self, tenant_id, company_id, subject_type, subject_id, pii_classes,
                  retention_class, days, *, hold):
        record_id = stable_id("retention", subject_type, subject_id)
        prior = self.repository.get_broker_record("communication_compliance_retention",
                                                  tenant_id, company_id, record_id)
        if prior:
            return prior
        now = self.clock()
        value = RetentionRecord(record_id, tenant_id, company_id, subject_type, subject_id,
                                tuple(pii_classes), retention_class, now,
                                now + timedelta(days=days), hold, False, None,
                                POLICY_VERSION)
        self.repository.save_broker_record("communication_compliance_retention", record_id,
                                           tenant_id, company_id, value)
        return value

    def classify_recipient(self, recipient) -> RetentionRecord:
        return self._classify(recipient.tenant_id, recipient.company_id, "recipient",
                              recipient.recipient_id,
                              (PIIClass.DESTINATION_IDENTIFIER, PIIClass.RECIPIENT_METADATA),
                              RetentionClass.OPERATIONAL_SHORT, 365, hold=False)

    def _retention_for(self, tenant_id, company_id, subject_type, subject_id):
        return self.repository.get_broker_record(
            "communication_compliance_retention", tenant_id, company_id,
            stable_id("retention", subject_type, subject_id))

    def _jurisdiction(self, tenant_id, company_id, recipient_id):
        return self.repository.get_broker_record(
            "communication_compliance_jurisdiction", tenant_id, company_id,
            self._jurisdiction_id(tenant_id, company_id, recipient_id)) or self.repository.get_broker_record(
                "communication_compliance_jurisdiction", tenant_id, company_id,
                self._jurisdiction_id(tenant_id, company_id, None))

    @staticmethod
    def _jurisdiction_id(tenant_id, company_id, recipient_id):
        return stable_id("jurisdiction", tenant_id, company_id, recipient_id or "all")

    def _rollout(self, tenant_id, company_id):
        record_id = stable_id("rollout", tenant_id, company_id)
        value = self.repository.get_broker_record("communication_compliance_rollout",
                                                  tenant_id, company_id, record_id)
        if value:
            return value
        value = RolloutPolicy(record_id, tenant_id, company_id, RolloutTier.SANDBOX,
                              (tenant_id,), (company_id,), tuple(sorted(self.sandbox_providers)),
                              tuple(CommunicationPurpose), ("example.test",), 100, 500, 100,
                              False, True, "server_default", self.clock())
        self.repository.save_broker_record("communication_compliance_rollout", record_id,
                                           tenant_id, company_id, value)
        return value

    def _killed(self, communication):
        keys = (
            (KillSwitchScope.GLOBAL, "global"),
            (KillSwitchScope.TENANT, communication.tenant_id),
            (KillSwitchScope.COMPANY, communication.company_id),
            (KillSwitchScope.PROVIDER_CONNECTION, communication.provider_connection_id),
            (KillSwitchScope.CHANNEL, DestinationType.EMAIL.value),
        )
        return any(value and value.engaged for value in (self._switch(*key) for key in keys))

    def _switch(self, scope, scope_id):
        return self.repository.get_broker_record_by_id(
            "communication_compliance_kill_switch", self._kill_id(scope, scope_id))

    @staticmethod
    def _kill_id(scope, scope_id):
        return stable_id("kill_switch", scope.value, scope_id)

    def _create_alert(self, tenant_id, company_id, alert_class, severity, reason, source_ref):
        alert_id = stable_id("alert", tenant_id, company_id, alert_class.value,
                             severity.value, source_ref)
        prior = self.repository.get_broker_record("communication_compliance_alert",
                                                  tenant_id, company_id, alert_id)
        if prior:
            return prior
        alert = OperationalAlert(alert_id, tenant_id, company_id, alert_class,
                                 severity, reason, source_ref, self.clock())
        self.repository.save_broker_record("communication_compliance_alert", alert_id,
                                           tenant_id, company_id, alert)
        self._audit(tenant_id, company_id, "compliance_monitor", "compliance.alert.created",
                    "alert", alert_id, reason,
                    {"class": alert_class.value, "severity": severity.value})
        if self.notification_adapter is not None:
            self.notification_adapter.notify(alert)
        return alert

    @staticmethod
    def _in_quiet_hours(hour: int, quiet_hours: tuple[int, int]) -> bool:
        start, end = quiet_hours
        if not (0 <= start <= 23 and 0 <= end <= 23):
            raise ComplianceDenied("jurisdiction quiet hours are invalid")
        return start <= hour < end if start < end else hour >= start or hour < end

    def _manage(self, principal, tenant_id, company_id):
        verified = self.principal_authority.verify(principal, tenant_id=tenant_id,
                                                   company_id=company_id)
        self.authorization.require(AuthorizationContext(
            verified.user_id, tenant_id, company_id,
            verified.support_impersonation_session_id),
            Permission.MANAGE_COMMUNICATIONS, at=self.clock())
        return verified

    def _view(self, principal, tenant_id, company_id):
        verified = self.principal_authority.verify(principal, tenant_id=tenant_id,
                                                   company_id=company_id)
        self.authorization.require(AuthorizationContext(
            verified.user_id, tenant_id, company_id,
            verified.support_impersonation_session_id),
            Permission.VIEW_COMPANY_STATE, at=self.clock())
        return verified

    @staticmethod
    def _reason(exc):
        text = str(exc).lower()
        for marker, code in (
            ("jurisdiction", "unsupported_jurisdiction"),
            ("consent", "consent_evidence_invalid"),
            ("kill switch", "kill_switch_engaged"),
            ("rollout", "rollout_denied"),
            ("provider", "live_provider_disabled"),
            ("purpose", "purpose_denied"),
        ):
            if marker in text:
                return code
        return "compliance_default_deny"

    def _audit(self, tenant_id, company_id, actor_id, action, target_type,
               target_id, reason, details):
        self.audit.record(
            tenant_id=tenant_id, company_id=company_id, actor_type="system",
            actor_id=actor_id, action=action, target_type=target_type,
            target_id=target_id,
            correlation_id=details.get("job_id") or self.id_factory("correlation"),
            reason=reason, source="communications_compliance_operations",
            details=details, after={"target_id": target_id, "action": action},
        )
