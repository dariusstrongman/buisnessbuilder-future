from __future__ import annotations

import hmac
import json
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Callable

from businessbuilder.access_broker.models import ConnectionStatus, ReceiptStatus, stable_id
from businessbuilder.commercial.models import EntitlementStatus
from businessbuilder.company_brain import LifecycleState, Scope
from businessbuilder.identity.models import OrganizationStatus, TenantStatus
from businessbuilder.outbound_communications.models import CommunicationPurpose, DeliveryStatus

from .models import (
    CanaryAlert, CanaryAlertSeverity, CanaryEligibility, CanaryMetric, CanaryPermit,
    CanaryPermitStatus, CanarySendReservation, DeliverabilityHealth, LiveGateCeremony,
    ReadinessState, ReconciliationState, ReconciliationTask, ReputationState,
    SenderIdentityReadiness,
)


class CanaryDenied(PermissionError):
    pass


class LiveCanaryReadiness:
    """Readiness/status gate below existing authorities; it never schedules or sends work."""

    def __init__(self, *, repository, identity_repository, commercial_repository,
                 compliance, company_brain, clock: Callable[[], datetime],
                 id_factory: Callable[[str], str], permit_signing_key: bytes,
                 provider_adapters: dict[str, object], operator_verifier,
                 founder_approval_verifier=None,
                 runbook_ref: str = "docs/LIVE_COMMUNICATIONS_CANARY_RUNBOOKS.md") -> None:
        if len(permit_signing_key) < 32:
            raise ValueError("canary permit signing key must be at least 32 bytes")
        self.repository = repository
        self.identity_repository = identity_repository
        self.commercial_repository = commercial_repository
        self.compliance = compliance
        self.company_brain = company_brain
        self.clock = clock
        self.id_factory = id_factory
        self._signing_key = permit_signing_key
        self.provider_adapters = provider_adapters
        self.operator_verifier = operator_verifier
        self.founder_approval_verifier = founder_approval_verifier
        self.runbook_ref = runbook_ref
        self.live_send_enabled = False
        compliance.canary_readiness = self

    def record_sender_readiness(self, operator_context: object, *, tenant_id: str,
                                company_id: str, provider_connection_id: str,
                                sender_address: str, domain_ownership_verified: bool,
                                spf_valid: bool, dkim_valid: bool, dmarc_present: bool,
                                alignment_valid: bool, provider_verified: bool,
                                reputation: ReputationState) -> SenderIdentityReadiness:
        actor = self._operator(operator_context)
        normalized = sender_address.strip().lower()
        if normalized.count("@") != 1 or any(c in normalized for c in "\r\n\0"):
            raise ValueError("sender address is invalid")
        domain = normalized.rsplit("@", 1)[1]
        connection = self.repository.get_broker_record(
            "provider_connection", tenant_id, company_id, provider_connection_id)
        if connection is None:
            raise CanaryDenied("provider connection is outside tenant/company scope")
        sender_id = stable_id("sender", tenant_id, company_id, provider_connection_id, normalized)
        value = SenderIdentityReadiness(
            sender_id, tenant_id, company_id, provider_connection_id, normalized, domain,
            domain_ownership_verified, spf_valid, dkim_valid, dmarc_present,
            alignment_valid, provider_verified, reputation, self.clock())
        self.repository.save_broker_record("live_canary_sender", sender_id,
                                           tenant_id, company_id, value)
        self._audit(tenant_id, company_id, actor, "canary.sender_readiness.recorded",
                    "sender", sender_id, "synthetic sender readiness recorded",
                    {"domain_auth_valid": value.domain_auth_valid,
                     "reputation": reputation.value})
        return value

    def record_deliverability(self, operator_context: object, *, tenant_id: str,
                              company_id: str, provider_connection_id: str,
                              delivery_success_rate: float = 1.0,
                              deferred_rate: float = 0.0, hard_bounce_rate: float = 0.0,
                              complaint_rate: float = 0.0, rejection_rate: float = 0.0,
                              provider_throttled: bool = False,
                              authentication_failures: int = 0,
                              reputation_warnings: tuple[str, ...] = ()) -> DeliverabilityHealth:
        actor = self._operator(operator_context)
        rates = (delivery_success_rate, deferred_rate, hard_bounce_rate,
                 complaint_rate, rejection_rate)
        if any(v < 0 or v > 1 for v in rates):
            raise ValueError("deliverability rates must be between zero and one")
        now = self.clock()
        health_id = stable_id("deliverability", tenant_id, company_id, provider_connection_id)
        value = DeliverabilityHealth(
            health_id, tenant_id, company_id, provider_connection_id,
            *rates, provider_throttled, authentication_failures,
            tuple(reputation_warnings), now if delivery_success_rate else None,
            now, now, now)
        self.repository.save_broker_record("live_canary_deliverability", health_id,
                                           tenant_id, company_id, value)
        self._audit(tenant_id, company_id, actor, "canary.deliverability.recorded",
                    "deliverability_health", health_id, "synthetic health recorded", {})
        return value

    def evaluate_eligibility(self, *, tenant_id: str, company_id: str,
                             provider_connection_id: str, sender_id: str,
                             approval_ref: str | None) -> CanaryEligibility:
        now = self.clock()
        gates: list[tuple[str, bool, str]] = []
        def gate(name: str, passed: bool, reason: str):
            gates.append((name, bool(passed), reason))

        try:
            tenant = self.identity_repository.get_tenant(tenant_id)
            organization = self.identity_repository.get_organization_by_tenant(tenant_id)
            gate("tenant_active", tenant.status is TenantStatus.ACTIVE, "tenant is not active")
            gate("organization_active", organization.status is OrganizationStatus.ACTIVE,
                 "organization is not active")
            company_in_scope = company_id in organization.company_ids
            try:
                brain_company = self.company_brain.get_company(Scope(tenant_id, company_id))
                company_in_scope = company_in_scope and brain_company.lifecycle not in {
                    LifecycleState.PAUSED, LifecycleState.ARCHIVED}
            except (KeyError, LookupError):
                company_in_scope = False
            gate("company_active", company_in_scope,
                 "company is not active in organization scope")
        except (KeyError, LookupError):
            gate("tenant_active", False, "tenant is unavailable")
            gate("organization_active", False, "organization is unavailable")
            gate("company_active", False, "company is unavailable")
        grants = self.commercial_repository.get_current_entitlement_grants(tenant_id, company_id)
        # BUILD_AND_RUN is represented by its managed execution grant, not by a
        # product-code-shaped entitlement record.
        entitlement_ok = any(g.entitlement_code == "ai_workforce.execute"
                             and g.status is EntitlementStatus.ACTIVE for g in grants)
        gate("build_and_run_entitlement", entitlement_ok, "BUILD_AND_RUN entitlement is not active")
        connection = self.repository.get_broker_record(
            "provider_connection", tenant_id, company_id, provider_connection_id)
        gate("provider_connection", connection is not None and connection.status is ConnectionStatus.ACTIVE,
             "provider connection is not active")
        adapter = self.provider_adapters.get(connection.provider) if connection else None
        required_scopes = adapter.contract.oauth_scopes if adapter else frozenset()
        gate("oauth_scopes", bool(connection) and bool(required_scopes)
             and required_scopes.issubset(connection.scopes_granted), "required OAuth scopes are missing")
        provider_health = self.repository.get_broker_record(
            "provider_health", tenant_id, company_id, provider_connection_id)
        gate("provider_health", provider_health is not None and provider_health.usable,
             "provider connection health is not usable")
        sender = self.repository.get_broker_record("live_canary_sender", tenant_id, company_id, sender_id)
        gate("sender_identity", sender is not None and sender.provider_connection_id == provider_connection_id,
             "sender identity is not bound to provider connection")
        gate("domain_authentication", sender is not None and sender.domain_auth_valid,
             "SPF, DKIM, DMARC, ownership, alignment, or provider verification failed")
        gate("sender_reputation", sender is not None and sender.reputation is ReputationState.GOOD,
             "sender reputation is not good")
        health_id = stable_id("deliverability", tenant_id, company_id, provider_connection_id)
        health = self.repository.get_broker_record("live_canary_deliverability", tenant_id,
                                                   company_id, health_id)
        gate("deliverability_health", health is not None and not health.provider_throttled
             and health.authentication_failures == 0 and health.complaint_rate == 0
             and health.hard_bounce_rate == 0 and not health.reputation_warnings,
             "deliverability health is degraded")
        jurisdiction = self.compliance._jurisdiction(tenant_id, company_id, None)
        if jurisdiction is None:
            jurisdictions = self.repository.list_broker_records(
                "communication_compliance_jurisdiction", tenant_id, company_id)
            jurisdiction = jurisdictions[0] if jurisdictions else None
        gate("compliance_policy", jurisdiction is not None,
             "jurisdiction compliance policy is not configured")
        consents = self.repository.list_broker_records("communication_compliance_consent",
                                                       tenant_id, company_id)
        gate("consent_policy", any(c.active_at(now) for c in consents),
             "no active attributable consent evidence exists")
        gate("unsubscribe_and_suppression", bool(self.compliance._unsubscribe_key),
             "unsubscribe or suppression controls are unavailable")
        gate("callback_authenticity", adapter is not None
             and bool(adapter.contract.callback_verification_method),
             "provider callback authenticity is not configured")
        gate("kill_switches", hasattr(self.compliance, "_killed"),
             "kill-switch enforcement is unavailable")
        # Use the existing explicit scope model without creating parallel control state.
        from businessbuilder.communications_compliance.models import KillSwitchScope, RolloutTier
        active_switch = any(value and value.engaged for value in (
            self.compliance._switch(KillSwitchScope.GLOBAL, "global"),
            self.compliance._switch(KillSwitchScope.TENANT, tenant_id),
            self.compliance._switch(KillSwitchScope.COMPANY, company_id),
            self.compliance._switch(KillSwitchScope.PROVIDER_CONNECTION, provider_connection_id),
            self.compliance._switch(KillSwitchScope.CHANNEL, "email"),
        ))
        gate("kill_switch_clear", not active_switch, "an outbound kill switch is engaged")
        rollout = self.compliance._rollout(tenant_id, company_id)
        gate("rollout_sandbox", rollout.tier is RolloutTier.SANDBOX,
             "rollout must remain sandbox during readiness")
        gate("alerts", hasattr(self.compliance, "record_abuse_signal"),
             "operational alerts are unavailable")
        thresholds = self.compliance.abuse_thresholds
        gate("abuse_thresholds", 0 < thresholds.warning <= thresholds.throttle
             <= thresholds.suspend <= thresholds.emergency,
             "abuse thresholds are missing or inconsistent")
        gate("operator_runbook", bool(self.runbook_ref), "operator runbook is unavailable")
        gate("live_gate_off", not self.live_send_enabled and not self.compliance.live_send_enabled,
             "live-send gate must remain off during readiness")
        gate("privileged_approval", bool(approval_ref), "privileged approval is not recorded")
        passed = all(value for _, value, _ in gates)
        evaluation = CanaryEligibility(
            self.id_factory("canary_evaluation"), tenant_id, company_id,
            provider_connection_id, sender_id,
            ReadinessState.CANARY_READY if passed else ReadinessState.NOT_READY,
            tuple(gates), bool(approval_ref), False, now)
        self.repository.save_broker_record("live_canary_eligibility", evaluation.evaluation_id,
                                           tenant_id, company_id, evaluation)
        self._audit(tenant_id, company_id, "canary_readiness", "canary.eligibility.evaluated",
                    "eligibility", evaluation.evaluation_id,
                    "canary ready" if passed else "canary gates failed",
                    {"state": evaluation.state.value,
                     "failed_gates": ",".join(name for name, ok, _ in gates if not ok),
                     "live_send_enabled": False})
        return evaluation

    def conduct_simulated_ceremony(self, operator_context: object, *, tenant_id: str,
                                   company_id: str, provider_connection_id: str,
                                   sender_id: str, recipient_digests: tuple[str, ...],
                                   recipient_domains: tuple[str, ...],
                                   allowed_purposes: tuple[CommunicationPurpose, ...],
                                   max_sends_total: int, max_sends_hour: int,
                                   starts_at: datetime, expires_at: datetime,
                                   monitoring_owner: str, rollback_plan_ref: str,
                                   kill_switch_test_ref: str,
                                   approval_ref: str) -> CanaryPermit:
        try:
            actor = self._operator(operator_context)
        except CanaryDenied:
            self._audit(tenant_id, company_id, "untrusted", "canary.permit.denied",
                        "canary_permit", "unresolved",
                        "trusted privileged operator context required",
                        {"live_send_enabled": False})
            raise
        if not recipient_digests or any(v == "*" for v in (*recipient_digests, *recipient_domains)):
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "wildcard or empty recipient scope is forbidden")
        if not allowed_purposes or any(p is not CommunicationPurpose.REPLY_TO_INBOUND
                                       for p in allowed_purposes):
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "first canary permits reply_to_inbound only")
        if max_sends_total < 1 or max_sends_total > 10 or max_sends_hour < 1 \
                or max_sends_hour > max_sends_total:
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "canary send limits are outside the tiny-pilot boundary")
        if (starts_at >= expires_at or expires_at <= self.clock()
                or expires_at - starts_at > timedelta(hours=24)):
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "canary permit window is invalid")
        if self.founder_approval_verifier is None or not self.founder_approval_verifier(
                tenant_id, company_id, approval_ref):
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "trusted founder approval is required")
        eligibility = self.evaluate_eligibility(
            tenant_id=tenant_id, company_id=company_id,
            provider_connection_id=provider_connection_id, sender_id=sender_id,
            approval_ref=approval_ref)
        if eligibility.state is not ReadinessState.CANARY_READY:
            self._ceremony_denied(tenant_id, company_id, actor,
                                  "canary eligibility gates failed")
        checklist = tuple((name, True) for name in (
            "target_scope", "provider", "sender", "purposes", "recipients", "send_caps",
            "time_window", "monitoring_owner", "rollback_plan", "kill_switch_test", "approval"))
        ceremony = LiveGateCeremony(
            self.id_factory("canary_ceremony"), tenant_id, company_id,
            provider_connection_id, sender_id, actor, monitoring_owner,
            rollback_plan_ref, kill_switch_test_ref, approval_ref, checklist,
            self.clock(), False)
        permit_id = self.id_factory("canary_permit")
        unsigned = {
            "permit_id": permit_id, "tenant_id": tenant_id, "company_id": company_id,
            "provider_connection_id": provider_connection_id, "sender_id": sender_id,
            "recipient_digests": sorted(recipient_digests),
            "recipient_domains": sorted(recipient_domains),
            "allowed_purposes": sorted(p.value for p in allowed_purposes),
            "max_sends_total": max_sends_total, "max_sends_hour": max_sends_hour,
            "starts_at": starts_at.isoformat(), "expires_at": expires_at.isoformat(),
            "operator_owner": actor, "founder_approval_ref": approval_ref,
            "ceremony_id": ceremony.ceremony_id, "simulation_only": True,
        }
        signature = hmac.new(self._signing_key, self._canonical(unsigned), sha256).hexdigest()
        permit = CanaryPermit(
            permit_id, tenant_id, company_id, provider_connection_id, sender_id,
            tuple(sorted(recipient_digests)), tuple(sorted(recipient_domains)),
            tuple(allowed_purposes), max_sends_total, max_sends_hour,
            starts_at, expires_at, actor, approval_ref, True, ceremony.ceremony_id,
            signature, CanaryPermitStatus.APPROVED_SIMULATION, True)
        with self.repository.transaction():
            self.repository.save_broker_record("live_canary_ceremony", ceremony.ceremony_id,
                                               tenant_id, company_id, ceremony)
            self.repository.save_broker_record("live_canary_permit", permit.permit_id,
                                               tenant_id, company_id, permit)
        self._audit(tenant_id, company_id, actor, "canary.ceremony.simulated",
                    "canary_permit", permit_id,
                    "simulation permit created without changing live gate",
                    {"simulation_only": True, "live_send_enabled": False,
                     "max_sends_total": max_sends_total})
        return permit

    def validate_simulated_send(self, communication, recipient, *, stage: str) -> None:
        if not communication.canary_permit_ref:
            return
        permit = self.repository.get_broker_record(
            "live_canary_permit", communication.tenant_id, communication.company_id,
            communication.canary_permit_ref)
        if permit is None or not self._valid_signature(permit):
            self._alert(communication.tenant_id, communication.company_id,
                        CanaryAlertSeverity.CRITICAL, "forged_permit", "permit_invalid",
                        communication.communication_id)
            raise CanaryDenied("canary permit is missing or forged")
        now = self.clock()
        if permit.status is not CanaryPermitStatus.APPROVED_SIMULATION or permit.revoked_at:
            raise CanaryDenied("canary permit is inactive")
        if not permit.simulation_only or self.live_send_enabled or self.compliance.live_send_enabled:
            raise CanaryDenied("live provider execution is disabled")
        if not (permit.starts_at <= now < permit.expires_at):
            if now >= permit.expires_at and permit.status is CanaryPermitStatus.APPROVED_SIMULATION:
                expired = replace(permit, status=CanaryPermitStatus.EXPIRED)
                self.repository.save_broker_record("live_canary_permit", permit.permit_id,
                                                   permit.tenant_id, permit.company_id, expired)
            raise CanaryDenied("canary permit expired or is not active")
        if (permit.tenant_id != communication.tenant_id
                or permit.company_id != communication.company_id
                or permit.provider_connection_id != communication.provider_connection_id
                or communication.purpose not in permit.allowed_purposes):
            raise CanaryDenied("communication is outside canary permit scope")
        sender = self.repository.get_broker_record("live_canary_sender", permit.tenant_id,
                                                   permit.company_id, permit.sender_id)
        if sender is None or sender.provider_connection_id != permit.provider_connection_id:
            raise CanaryDenied("sender is outside canary permit scope")
        domain = recipient.normalized_destination.rsplit("@", 1)[-1]
        if recipient.destination_digest not in permit.recipient_digests \
                or (permit.recipient_domains and domain not in permit.recipient_domains):
            self._alert(permit.tenant_id, permit.company_id, CanaryAlertSeverity.CRITICAL,
                        "unexpected_recipient", "recipient_not_allowlisted",
                        communication.communication_id)
            raise CanaryDenied("recipient is outside explicit canary allowlist")
        eligibility = self.evaluate_eligibility(
            tenant_id=permit.tenant_id, company_id=permit.company_id,
            provider_connection_id=permit.provider_connection_id,
            sender_id=permit.sender_id, approval_ref=permit.founder_approval_ref)
        if eligibility.state is not ReadinessState.CANARY_READY:
            raise CanaryDenied("current canary readiness gates failed")
        self._metric(permit.tenant_id, permit.company_id,
                     "canary_admission_allowed" if stage == "admission" else "canary_execution_allowed",
                     communication.communication_id)
        if stage == "execution":
            reservation = CanarySendReservation(
                stable_id("canary_reservation", permit.permit_id, communication.communication_id),
                permit.permit_id, permit.tenant_id, permit.company_id,
                communication.communication_id,
                stable_id("canary_send", permit.permit_id, communication.runtime_idempotency_key), now)
            _, allowed, reason = self.repository.reserve_canary_send(
                reservation, permit.max_sends_total, permit.max_sends_hour)
            if not allowed and reason != "duplicate":
                self._alert(permit.tenant_id, permit.company_id, CanaryAlertSeverity.WARNING,
                            "send_cap", reason.replace(" ", "_"), communication.communication_id)
                raise CanaryDenied(reason)

    def revoke_permit(self, operator_context: object, *, tenant_id: str,
                      company_id: str, permit_id: str, reason: str) -> CanaryPermit:
        actor = self._operator(operator_context)
        permit = self.repository.get_broker_record("live_canary_permit", tenant_id, company_id, permit_id)
        if permit is None:
            raise CanaryDenied("canary permit is outside scope")
        changed = replace(permit, status=CanaryPermitStatus.REVOKED, revoked_at=self.clock())
        self.repository.save_broker_record("live_canary_permit", permit_id,
                                           tenant_id, company_id, changed)
        self._audit(tenant_id, company_id, actor, "canary.rollback.completed",
                    "canary_permit", permit_id, reason, {"live_send_enabled": False})
        return changed

    def reconcile_uncertain(self, *, tenant_id: str, company_id: str,
                            receipt_id: str, adapter_name: str):
        receipts = self.repository.list_provider_receipts(tenant_id, company_id)
        receipt = next((v for v in receipts if v.receipt_id == receipt_id), None)
        adapter = self.provider_adapters.get(adapter_name)
        if receipt is None or adapter is None or receipt.provider != adapter_name:
            raise CanaryDenied("receipt or provider is outside reconciliation scope")
        now = self.clock()
        result = adapter.reconcile(receipt.provider_request_id)
        task_id = stable_id("reconcile", receipt.receipt_id)
        task = ReconciliationTask(
            task_id, tenant_id, company_id, receipt.connection_id or "connection_unknown",
            receipt.provider_request_id, receipt.receipt_id, result.state, 1, now, now)
        self.repository.save_broker_record("live_canary_reconciliation", task_id,
                                           tenant_id, company_id, task)
        if result.state is ReconciliationState.RESOLVED:
            self.repository.complete_provider_receipt(replace(
                receipt, status=ReceiptStatus.SUCCEEDED, retryable=False,
                response_classification=result.response_classification,
                external_object_ref=result.external_object_ref,
                reconciliation_status="resolved", completed_at=now))
        self._metric(tenant_id, company_id, "reconciliation_resolved"
                     if result.state is ReconciliationState.RESOLVED else "reconciliation_backlog",
                     receipt_id)
        return task

    def customer_status(self, principal, *, tenant_id: str, company_id: str) -> dict:
        base = self.compliance.customer_status(principal, tenant_id=tenant_id, company_id=company_id)
        senders = self.repository.list_broker_records("live_canary_sender", tenant_id, company_id)
        if base["sending"] == "paused":
            state = "Sending paused"
        elif not senders:
            state = "Needs domain setup"
        elif base["connection"] == "needs_reconnect":
            state = "Needs reconnect"
        else:
            state = "Canary review pending"
        return {"provider": base["connection"], "mode": "Sandbox testing", "status": state,
                "live_send_enabled": False}

    def operator_readiness(self, operator_context: object, *, tenant_id: str,
                           company_id: str, provider_connection_id: str,
                           sender_id: str, approval_ref: str | None) -> dict:
        self._operator(operator_context)
        result = self.evaluate_eligibility(
            tenant_id=tenant_id, company_id=company_id,
            provider_connection_id=provider_connection_id,
            sender_id=sender_id, approval_ref=approval_ref)
        return {"state": result.state.value,
                "provider_connection_id": provider_connection_id,
                "sender_id": sender_id,
                "rollout_tier": self.compliance._rollout(tenant_id, company_id).tier.value,
                "alerts": len(self.repository.list_broker_records(
                    "live_canary_alert", tenant_id, company_id)),
                "failed_gates": [{"gate": name, "reason": reason}
                                 for name, passed, reason in result.gates if not passed],
                "live_send_enabled": False,
                "theoretically_approvable": result.state is ReadinessState.CANARY_READY}

    def _valid_signature(self, permit: CanaryPermit) -> bool:
        unsigned = {
            "permit_id": permit.permit_id, "tenant_id": permit.tenant_id,
            "company_id": permit.company_id,
            "provider_connection_id": permit.provider_connection_id,
            "sender_id": permit.sender_id,
            "recipient_digests": sorted(permit.recipient_digests),
            "recipient_domains": sorted(permit.recipient_domains),
            "allowed_purposes": sorted(p.value for p in permit.allowed_purposes),
            "max_sends_total": permit.max_sends_total,
            "max_sends_hour": permit.max_sends_hour,
            "starts_at": permit.starts_at.isoformat(), "expires_at": permit.expires_at.isoformat(),
            "operator_owner": permit.operator_owner,
            "founder_approval_ref": permit.founder_approval_ref,
            "ceremony_id": permit.ceremony_id, "simulation_only": True,
        }
        expected = hmac.new(self._signing_key, self._canonical(unsigned), sha256).hexdigest()
        return hmac.compare_digest(expected, permit.signature)

    @staticmethod
    def _canonical(value: dict) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def _operator(self, context) -> str:
        actor = self.operator_verifier(context) if self.operator_verifier else ""
        if not actor:
            raise CanaryDenied("trusted privileged operator context required")
        return actor

    def _ceremony_denied(self, tenant_id, company_id, actor, reason):
        self._audit(tenant_id, company_id, actor, "canary.permit.denied",
                    "canary_permit", "unresolved", reason,
                    {"live_send_enabled": False})
        raise CanaryDenied(reason)

    def _metric(self, tenant_id, company_id, name, source_ref):
        metric = CanaryMetric(self.id_factory("canary_metric"), tenant_id, company_id,
                              name, 1, source_ref, self.clock())
        self.repository.save_broker_record("live_canary_metric", metric.metric_id,
                                           tenant_id, company_id, metric)
        return metric

    def _alert(self, tenant_id, company_id, severity, alert_type, reason, source_ref):
        alert = CanaryAlert(stable_id("canary_alert", tenant_id, company_id, alert_type,
                                      source_ref), tenant_id, company_id, severity,
                            alert_type, reason, source_ref, self.clock())
        self.repository.save_broker_record("live_canary_alert", alert.alert_id,
                                           tenant_id, company_id, alert)
        return alert

    def _audit(self, tenant_id, company_id, actor_id, action, target_type,
               target_id, reason, metadata):
        self.compliance._audit(tenant_id, company_id, actor_id, action, target_type,
                               target_id, reason, metadata)
