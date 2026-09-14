from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Callable, TYPE_CHECKING

from businessbuilder.access_broker import ArtifactClassification, ArtifactStatus, ReceiptStatus
from businessbuilder.company_brain import (
    CompanyBrainService,
    EntityRef,
    KnowledgeClass,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.runtime import ArtifactRef, Capability, CapabilityRequest, CapabilityResult, Money
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.verification import (
    EvidenceRef,
    EvidenceType,
    VerificationMethod,
    VerificationRecord,
    VerificationService,
    VerificationState,
)
from businessbuilder.verification.catalog import VerificationDefinition

if TYPE_CHECKING:
    from .evidence_review import ResidentialCleaningEvidenceReviewService


TRANSITION_CAPABILITY = "pilot.residential_cleaning.founder_action.transition"
VERIFY_CAPABILITY = "pilot.residential_cleaning.founder_action.verify"
EVIDENCE_CAPABILITY = "pilot.residential_cleaning.founder_action.evidence"
REVIEW_TEST = "cleaning-founder-action-review"


class FounderActionStage(StrEnum):
    PREPARED = "prepared"
    EXPLAINED = "explained"
    LINKED = "linked"
    FOUNDER_COMPLETED = "founder_completed"
    RESULT_CAPTURED = "result_captured"
    VERIFIED = "verified"


_STAGE_ORDER = {
    FounderActionStage.PREPARED: 1,
    FounderActionStage.EXPLAINED: 2,
    FounderActionStage.LINKED: 3,
    FounderActionStage.FOUNDER_COMPLETED: 4,
    FounderActionStage.RESULT_CAPTURED: 5,
    FounderActionStage.VERIFIED: 6,
}


@dataclass(frozen=True, slots=True)
class FounderActionDefinition:
    key: str
    title: str
    reason: str
    checklist: tuple[str, ...]
    destination_label: str
    destination_url: str | None
    destination_kind: str
    authority_code: str
    required_evidence: frozenset[EvidenceType]
    provider_operation: str | None = None
    critical: bool = True

    @property
    def action_id(self) -> str:
        return f"founder_action_cleaning_{self.key}"

    @property
    def verification_definition_id(self) -> str:
        return f"cleaning.founder_action.{self.key}"


def _definition(
    key: str,
    title: str,
    reason: str,
    checklist: tuple[str, ...],
    destination_label: str,
    destination_url: str | None,
    destination_kind: str,
    authority_code: str,
    evidence: tuple[EvidenceType, ...],
    *,
    provider_operation: str | None = None,
    critical: bool = True,
) -> FounderActionDefinition:
    return FounderActionDefinition(
        key,
        title,
        reason,
        checklist,
        destination_label,
        destination_url,
        destination_kind,
        authority_code,
        frozenset({EvidenceType.TEST_RESULT, EvidenceType.FOUNDER_ATTESTATION, *evidence}),
        provider_operation,
        critical,
    )


FOUNDER_ACTION_DEFINITIONS = (
    _definition(
        "entity_admin",
        "Choose and complete the entity/admin path",
        "Only the founder can choose the legal structure, make attestations, sign, name a registered agent, and pay an authority fee.",
        (
            "Review the prepared scope and decide whether to operate as an individual or form an entity with qualified advice where needed.",
            "Confirm the legal name, ownership, organizer, registered-agent consent, and address before submission.",
            "Submit only through the current Texas authority path and retain its filing confirmation.",
        ),
        "Texas Secretary of State — business services",
        "https://www.sos.state.tx.us/corp/do-business.shtml",
        "official_authority",
        "texas_secretary_of_state",
        (EvidenceType.AUTHORITY_CONFIRMATION,),
    ),
    _definition(
        "ein_tax_id",
        "Complete the EIN/tax-ID determination and application",
        "The responsible party must provide protected identity information and attest to an IRS application; Business Builder must not collect it.",
        (
            "Confirm the entity or individual tax posture before applying.",
            "Use the IRS application directly; never enter an SSN or EIN into Business Builder.",
            "Retain an opaque reference to the IRS result without uploading protected taxpayer identifiers.",
        ),
        "Internal Revenue Service — EIN",
        "https://www.irs.gov/businesses/small-businesses-self-employed/get-an-employer-identification-number",
        "official_authority",
        "internal_revenue_service",
        (EvidenceType.AUTHORITY_CONFIRMATION,),
    ),
    _definition(
        "bank",
        "Open the founder-owned business bank account",
        "A regulated financial institution must identify and approve the customer; Business Builder cannot attest, accept terms, or open it for the founder.",
        (
            "Compare eligible institutions and confirm deposit-insurance status where applicable.",
            "Apply directly with the selected institution and complete its identity checks.",
            "Return only a safe provider receipt reference; never provide credentials or account numbers.",
        ),
        "FDIC BankFind — institution research",
        "https://banks.data.fdic.gov/bankfind-suite/bankfind",
        "official_research_then_provider",
        "regulated_bank",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="bank.account_authorized",
    ),
    _definition(
        "insurance",
        "Choose and verify appropriate business insurance",
        "Coverage selection, representations, acceptance, and payment belong to the founder and a licensed insurance provider.",
        (
            "Review cleaning-specific premises, property-damage, key-custody, vehicle, worker, and liability risks with a qualified provider.",
            "Verify the provider or agent through the Texas regulator where applicable.",
            "Return only a safe policy/provider receipt reference, excluding policy secrets and payment data.",
        ),
        "Texas Department of Insurance — company and license lookup",
        "https://agate.tdi.texas.gov/consumer/company-profiles-and-agents-for-service-of-process.html",
        "official_research_then_provider",
        "licensed_insurance_provider",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="insurance.coverage_confirmed",
    ),
    _definition(
        "licenses_permits",
        "Confirm and complete required licenses and permits",
        "Requirements depend on the exact services, address, service area, and government interpretation, so the relevant authorities must confirm them.",
        (
            "Confirm the final service scope and operating address.",
            "Check the Texas permit guide and City of Denton permit/license resources.",
            "Capture an authority result for each applicable requirement, including an explicit no-license-required result when that is the authority response.",
        ),
        "Texas Business Permit Office and City of Denton",
        "https://gov.texas.gov/business/page/business-permits-office",
        "official_authority",
        "texas_denton_permit_authorities",
        (EvidenceType.AUTHORITY_CONFIRMATION,),
    ),
    _definition(
        "domain",
        "Authorize and prove founder ownership of the business domain",
        "The founder must own the registrar account, accept its terms, approve payment, and retain recovery control.",
        (
            "Review the approved brand/name before purchasing anything.",
            "Use a founder-owned registrar account with MFA and recovery controls.",
            "Return a safe provider receipt reference; DNS control will be independently tested later.",
        ),
        "Business Builder provider-connection handoff",
        "/api/v1/companies/{company_id}/provider-connections",
        "internal_provider_handoff",
        "domain_registrar",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="domain.ownership_authorized",
    ),
    _definition(
        "business_email",
        "Authorize the founder-owned business email provider",
        "The founder must accept mailbox terms, control account recovery, and authorize the minimum scopes.",
        (
            "Create the mailbox only in a founder-owned provider account.",
            "Enable MFA and preserve founder-controlled recovery.",
            "Authorize only the scopes required by the approved provider connection.",
        ),
        "Business Builder provider-connection handoff",
        "/api/v1/companies/{company_id}/provider-connections",
        "internal_provider_handoff",
        "email_provider",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="email.account_authorized",
    ),
    _definition(
        "crm",
        "Authorize the founder-owned CRM",
        "The founder must own the customer system and authorize its terms and access scope.",
        (
            "Confirm that the founder owns the CRM workspace and recovery path.",
            "Create the approved new/contacted/quoted/booked/lost stages.",
            "Authorize only the scoped Business Builder connection.",
        ),
        "Business Builder provider-connection handoff",
        "/api/v1/companies/{company_id}/provider-connections",
        "internal_provider_handoff",
        "crm_provider",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="crm.account_authorized",
        critical=False,
    ),
    _definition(
        "scheduling",
        "Authorize the founder-owned scheduling/calendar provider",
        "The founder controls calendar access, availability, cancellation rules, and provider terms.",
        (
            "Confirm launch availability and blackout periods.",
            "Configure service duration, travel buffer, rescheduling, and cancellation rules.",
            "Authorize only the scoped calendar connection.",
        ),
        "Business Builder provider-connection handoff",
        "/api/v1/companies/{company_id}/provider-connections",
        "internal_provider_handoff",
        "scheduling_provider",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="scheduling.account_authorized",
    ),
    _definition(
        "payments",
        "Authorize and verify the merchant payment provider",
        "The provider must complete business and founder verification, approve the account, and accept payout details directly from the founder.",
        (
            "Apply in the founder-owned provider account using the approved legal business facts.",
            "Complete provider identity and bank verification directly with the provider.",
            "Return only a safe provider receipt reference; never submit bank or identity data to Business Builder.",
        ),
        "Business Builder provider-connection handoff",
        "/api/v1/companies/{company_id}/provider-connections",
        "internal_provider_handoff",
        "payment_provider",
        (EvidenceType.PROVIDER_RECEIPT,),
        provider_operation="payments.account_authorized",
    ),
    _definition(
        "legal_name_address",
        "Confirm the legal name and business address",
        "Only the founder can attest that the identifying business facts are current and authorized for downstream use.",
        (
            "Compare the proposed legal name and address with current authority/provider records.",
            "Correct Company Brain facts before attesting if they differ.",
            "Attest only after the facts match; do not submit protected identity documents.",
        ),
        "Business Builder founder attestation",
        None,
        "internal_founder_attestation",
        "founder",
        (),
    ),
)


DEFINITIONS_BY_ID = {item.action_id: item for item in FOUNDER_ACTION_DEFINITIONS}


def register_verification_definitions(service: VerificationService) -> None:
    existing = {item.definition_id for item in service.registry.all()}
    for action in FOUNDER_ACTION_DEFINITIONS:
        if action.verification_definition_id in existing:
            continue
        service.registry.register(
            VerificationDefinition(
                definition_id=action.verification_definition_id,
                description=f"Residential-cleaning founder action evidence review: {action.title}",
                allowed_methods=frozenset({VerificationMethod.AUTOMATED_PLUS_HUMAN}),
                defined_tests=frozenset({REVIEW_TEST}),
                required_evidence_types=action.required_evidence,
                default_ttl_seconds=None,
            )
        )


def prepared_action_data(
    definition: FounderActionDefinition,
    *,
    company_id: str,
    prepared_at: str,
    actor_id: str,
) -> dict[str, Any]:
    destination_url = (
        definition.destination_url.format(company_id=company_id)
        if definition.destination_url
        else None
    )
    return {
        "action_key": definition.key,
        "title": definition.title,
        "reason": definition.reason,
        "responsibility": "FOUNDER_ACTION",
        "partner_authority": "EXTERNAL_PROVIDER/AUTHORITY",
        "authority_target": definition.authority_code,
        "prepared_data": {"checklist": list(definition.checklist)},
        "destination": {
            "label": definition.destination_label,
            "url": destination_url,
            "kind": definition.destination_kind,
            "launch_mode": "prepared_handoff_only",
        },
        "required_evidence_kinds": sorted(item.value for item in definition.required_evidence),
        "evidence": [],
        "evidence_refs": [],
        "verification": {
            "authority": "verification",
            "state": "not_requested",
            "verification_id": None,
            "result": None,
        },
        "state": FounderActionStage.PREPARED.value,
        "selected": True,
        "critical": definition.critical,
        "risk": "high" if definition.critical else "medium",
        "irreversible": definition.critical,
        "blocks": ["fully_set", *( ["ready"] if definition.critical else [] )],
        "timestamps": {FounderActionStage.PREPARED.value: prepared_at},
        "last_actor": {"type": "runtime", "id": actor_id},
        "history": [
            {
                "state": FounderActionStage.PREPARED.value,
                "at": prepared_at,
                "actor_type": "runtime",
                "actor_id": actor_id,
                "reason": "Business Builder prepared the scoped checklist and destination",
            }
        ],
    }


class ResidentialCleaningFounderActionTransitionCapability(Capability):
    name = TRANSITION_CAPABILITY
    version = "v1"

    def __init__(
        self,
        company_brain: CompanyBrainService,
        runtime_repository: RuntimeRepository,
        clock: Callable[[], datetime],
        evidence_reviews: ResidentialCleaningEvidenceReviewService | None = None,
    ) -> None:
        self.company_brain = company_brain
        self.runtime_repository = runtime_repository
        self.clock = clock
        self.evidence_reviews = evidence_reviews

    def validate_request(self, request: CapabilityRequest) -> None:
        if request.capability != self.name or request.capability_version != self.version:
            raise ValueError("founder-action transition capability mismatch")
        if request.inputs.get("operation") not in {
            "explain", "launch", "founder_complete", "capture_result", "capture_reviewed_result"
        }:
            raise ValueError("unsupported founder-action operation")
        if request.inputs.get("action_id") not in DEFINITIONS_BY_ID:
            raise PermissionError("action is outside the residential-cleaning pilot")
        if not isinstance(request.inputs.get("actor_id"), str):
            raise ValueError("server-derived actor is required")

    def estimate(self, request: CapabilityRequest) -> Money:
        self.validate_request(request)
        return Money("USD", 0)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        self.validate_request(request)
        scope = Scope(request.tenant_id, request.company_id)
        action = self.company_brain.repository.get_record(scope, request.inputs["action_id"])
        if action.kind is not RecordKind.FOUNDER_ACTION:
            raise PermissionError("record is not a founder action")
        operation = request.inputs["operation"]
        target = {
            "explain": FounderActionStage.EXPLAINED,
            "launch": FounderActionStage.LINKED,
            "founder_complete": FounderActionStage.FOUNDER_COMPLETED,
            "capture_result": FounderActionStage.RESULT_CAPTURED,
            "capture_reviewed_result": FounderActionStage.RESULT_CAPTURED,
        }[operation]
        current = FounderActionStage(action.data["state"])
        if _STAGE_ORDER[current] > _STAGE_ORDER[target]:
            if operation in {"capture_result", "capture_reviewed_result"}:
                supplied = self._references(request.inputs.get("evidence_refs", []))
                existing = {
                    (item["source_class"], item["reference"])
                    for item in action.data["evidence"]
                }
                if not supplied.issubset(existing):
                    raise PermissionError("captured evidence is immutable")
            return self._result(action.record_id, operation)
        if current is target:
            if operation in {"capture_result", "capture_reviewed_result"}:
                supplied = self._references(request.inputs.get("evidence_refs", []))
                existing = {(item["source_class"], item["reference"]) for item in action.data["evidence"]}
                if not supplied.issubset(existing):
                    raise PermissionError("captured evidence is immutable")
            return self._result(action.record_id, operation)
        predecessor = {
            FounderActionStage.EXPLAINED: FounderActionStage.PREPARED,
            FounderActionStage.LINKED: FounderActionStage.EXPLAINED,
            FounderActionStage.FOUNDER_COMPLETED: FounderActionStage.LINKED,
            FounderActionStage.RESULT_CAPTURED: FounderActionStage.FOUNDER_COMPLETED,
        }[target]
        if current is not predecessor:
            raise PermissionError("founder action stages cannot be skipped")

        now = self.clock()
        stamp = _iso(now)
        data = dict(action.data)
        evidence = [dict(item) for item in data.get("evidence", [])]
        if operation == "founder_complete":
            evidence.append(self._founder_attestation(action.record_id, request.inputs["actor_id"], stamp))
        elif operation in {"capture_result", "capture_reviewed_result"}:
            if operation == "capture_reviewed_result":
                if self.evidence_reviews is None:
                    raise PermissionError("operator-reviewed evidence boundary is unavailable")
                expected_refs, review_ids = self.evidence_reviews.active_accepted_references(
                    scope.tenant_id, scope.company_id, action.record_id
                )
                supplied = self._references(request.inputs.get("evidence_refs", []))
                expected = self._references(expected_refs)
                if supplied != expected or request.inputs.get("review_ids") != list(review_ids):
                    raise PermissionError("Runtime evidence set is not bound to current accepted reviews")
            evidence.extend(
                self._resolve_evidence(scope, action.record_id, request.inputs.get("evidence_refs", []), stamp)
            )
            evidence = list({item["evidence_id"]: item for item in evidence}.values())
            required = set(action.data["required_evidence_kinds"]) - {EvidenceType.TEST_RESULT.value}
            available = {item["evidence_type"] for item in evidence}
            missing = required - available
            if missing:
                raise PermissionError("required action evidence is missing")
        history = [*data.get("history", []), {
            "state": target.value,
            "at": stamp,
            "actor_type": (
                "operator" if operation == "capture_reviewed_result"
                else "founder" if operation in {"founder_complete", "capture_result"}
                else "runtime"
            ),
            "actor_id": request.inputs["actor_id"] if operation in {"founder_complete", "capture_result", "capture_reviewed_result"} else request.job_id,
            "reason": {
                "explain": "Founder was shown the scoped reason, checklist, evidence requirements, and uncertainty",
                "launch": "Prepared handoff destination was issued without performing the external action",
                "founder_complete": "Founder attested that the external or founder-controlled step was completed",
                "capture_result": "Scoped result references were validated and captured",
                "capture_reviewed_result": "Runtime captured only the current operator-accepted evidence set",
            }[operation],
        }]
        changed = {
            **data,
            "state": target.value,
            "evidence": evidence,
            "evidence_refs": [item["reference"] for item in evidence],
            "timestamps": {**data.get("timestamps", {}), target.value: stamp},
            "last_actor": {
                "type": (
                    "operator" if operation == "capture_reviewed_result"
                    else "founder" if operation in {"founder_complete", "capture_result"}
                    else "runtime"
                ),
                "id": request.inputs["actor_id"] if operation in {"founder_complete", "capture_result", "capture_reviewed_result"} else request.job_id,
            },
            "history": history,
        }
        self.company_brain.update_approved_state(
            scope,
            record_id=action.record_id,
            kind=action.kind,
            data=changed,
            knowledge_class=(
                KnowledgeClass.EXTERNAL_VERIFICATION
                if operation == "capture_reviewed_result"
                else KnowledgeClass.FOUNDER_DECISION
                if operation in {"founder_complete", "capture_result"}
                else action.knowledge_class
            ),
            provenance=action.provenance,
            confidence=None,
            owner_ref=action.owner_ref,
            expected_version=action.version,
        )
        return self._result(action.record_id, operation)

    def status(self, provider_ref: str) -> str:
        return "completed" if provider_ref.startswith("company-brain:founder-action:") else "unknown"

    def cancel(self, provider_ref: str) -> bool:
        return False

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        return None

    @staticmethod
    def _result(action_id: str, operation: str) -> CapabilityResult:
        return CapabilityResult(
            provider_ref=f"company-brain:founder-action:{action_id}:{operation}",
            status="completed",
            artifacts=(ArtifactRef("pilot_founder_action_state", action_id),),
            spend=Money("USD", 0),
            progress=(f"founder_action_{operation}",),
        )

    @staticmethod
    def _references(values: object) -> set[tuple[str, str]]:
        if not isinstance(values, list) or len(values) > 10:
            raise ValueError("evidence_refs must be a bounded list")
        result: set[tuple[str, str]] = set()
        for item in values:
            if not isinstance(item, dict) or set(item) != {"kind", "reference"}:
                raise ValueError("evidence reference is invalid")
            kind, reference = item["kind"], item["reference"]
            if kind not in {"artifact", "provider_receipt"} or not isinstance(reference, str) or not 3 <= len(reference) <= 160:
                raise ValueError("evidence reference is invalid")
            result.add((kind, reference))
        return result

    def _resolve_evidence(
        self,
        scope: Scope,
        action_id: str,
        values: object,
        captured_at: str,
    ) -> list[dict[str, Any]]:
        definition = DEFINITIONS_BY_ID[action_id]
        resolved: list[dict[str, Any]] = []
        for kind, reference in sorted(self._references(values)):
            if kind == "artifact":
                artifact = self.runtime_repository.get_broker_record(
                    "artifact", scope.tenant_id, scope.company_id, reference
                )
                if artifact is None or artifact.status is not ArtifactStatus.AVAILABLE:
                    raise PermissionError("evidence artifact is outside company scope or unavailable")
                if (
                    artifact.classification is ArtifactClassification.VERIFICATION_EVIDENCE
                    and artifact.provenance_ref == f"verified_authority:{definition.authority_code}"
                ):
                    evidence_type = EvidenceType.AUTHORITY_CONFIRMATION
                    issuer = definition.authority_code
                elif artifact.classification is ArtifactClassification.SCREENSHOT:
                    evidence_type = EvidenceType.SCREENSHOT
                    issuer = None
                else:
                    raise PermissionError("artifact classification cannot support this action")
                digest = artifact.content_sha256
            else:
                receipt = next(
                    (
                        item
                        for item in self.runtime_repository.list_provider_receipts(
                            scope.tenant_id, scope.company_id
                        )
                        if item.receipt_id == reference
                    ),
                    None,
                )
                if (
                    receipt is None
                    or receipt.status is not ReceiptStatus.SUCCEEDED
                    or receipt.capability != EVIDENCE_CAPABILITY
                    or receipt.operation != definition.provider_operation
                ):
                    raise PermissionError("provider result is not authentic for this action")
                evidence_type = EvidenceType.PROVIDER_RECEIPT
                issuer = receipt.provider
                digest = _digest(receipt.receipt_id, receipt.provider_request_id, receipt.status.value)
            resolved.append(
                {
                    "evidence_id": "evidence_" + _digest(action_id, kind, reference).split(":", 1)[1][:24],
                    "evidence_type": evidence_type.value,
                    "source_class": kind,
                    "reference": reference,
                    "content_digest": digest if digest.startswith("sha256:") else f"sha256:{digest}",
                    "issuer": issuer,
                    "captured_at": captured_at,
                }
            )
        return resolved

    @staticmethod
    def _founder_attestation(action_id: str, actor_id: str, captured_at: str) -> dict[str, Any]:
        digest = _digest(action_id, actor_id, captured_at, "completed_as_described")
        return {
            "evidence_id": "evidence_founder_" + digest.split(":", 1)[1][:24],
            "evidence_type": EvidenceType.FOUNDER_ATTESTATION.value,
            "source_class": "founder_assertion",
            "reference": f"company-brain:{action_id}:founder-attestation",
            "content_digest": digest,
            "issuer": actor_id,
            "captured_at": captured_at,
        }


class DeterministicResidentialCleaningEvidenceVerifier:
    """Explicit test-only verifier; it performs no network or provider action."""

    verifier_id = "business_builder_deterministic_test_verifier"

    def review(self, action: dict[str, Any], *, at: datetime) -> EvidenceRef:
        required = set(action["required_evidence_kinds"]) - {EvidenceType.TEST_RESULT.value}
        available = {item["evidence_type"] for item in action["evidence"]}
        if not required.issubset(available):
            raise PermissionError("deterministic review cannot replace missing evidence")
        digest = evidence_digest(action["evidence"])
        return EvidenceRef(
            evidence_id="evidence_test_" + digest.split(":", 1)[1][:24],
            evidence_type=EvidenceType.TEST_RESULT,
            artifact_ref=f"deterministic-test:{digest}",
            tenant_id="",
            company_id="",
            captured_at=at,
            test_name=REVIEW_TEST,
            test_passed=True,
            issuer=self.verifier_id,
            provenance={"mode": "deterministic_test_only", "evidence_digest": digest},
        )


class ResidentialCleaningReviewedEvidenceVerifier:
    """Server verifier for evidence already accepted through the operator-review boundary."""

    verifier_id = "business_builder_reviewed_evidence_verifier"

    def review(self, action: dict[str, Any], *, at: datetime) -> EvidenceRef:
        required = set(action["required_evidence_kinds"]) - {EvidenceType.TEST_RESULT.value}
        available = {item["evidence_type"] for item in action["evidence"]}
        if not required.issubset(available):
            raise PermissionError("operator review cannot replace missing action evidence")
        digest = evidence_digest(action["evidence"])
        return EvidenceRef(
            evidence_id="evidence_integrity_" + digest.split(":", 1)[1][:24],
            evidence_type=EvidenceType.TEST_RESULT,
            artifact_ref=f"reviewed-evidence-integrity:{digest}",
            tenant_id="",
            company_id="",
            captured_at=at,
            test_name=REVIEW_TEST,
            test_passed=True,
            issuer=self.verifier_id,
            provenance={
                "mode": "operator_review_plus_integrity_revalidation",
                "evidence_digest": digest,
            },
        )


class ResidentialCleaningFounderActionVerificationCapability(Capability):
    name = VERIFY_CAPABILITY
    version = "v1"

    def __init__(
        self,
        company_brain: CompanyBrainService,
        verification: VerificationService,
        verifier: DeterministicResidentialCleaningEvidenceVerifier | ResidentialCleaningReviewedEvidenceVerifier,
        runtime_repository: RuntimeRepository,
        clock: Callable[[], datetime],
        evidence_reviews: ResidentialCleaningEvidenceReviewService | None = None,
    ) -> None:
        self.company_brain = company_brain
        self.verification = verification
        self.verifier = verifier
        self.runtime_repository = runtime_repository
        self.clock = clock
        self.evidence_reviews = evidence_reviews

    def validate_request(self, request: CapabilityRequest) -> None:
        if request.capability != self.name or request.capability_version != self.version:
            raise ValueError("founder-action verification capability mismatch")
        if request.inputs.get("action_id") not in DEFINITIONS_BY_ID:
            raise PermissionError("action is outside the residential-cleaning pilot")

    def estimate(self, request: CapabilityRequest) -> Money:
        self.validate_request(request)
        return Money("USD", 0)

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        self.validate_request(request)
        scope = Scope(request.tenant_id, request.company_id)
        action = self.company_brain.repository.get_record(scope, request.inputs["action_id"])
        data = dict(action.data)
        current = FounderActionStage(data["state"])
        current_digest = evidence_digest(data.get("evidence", []))
        verification_id = f"verification_{action.record_id}"
        definition = DEFINITIONS_BY_ID[action.record_id]
        now = self.clock()
        if current is FounderActionStage.VERIFIED:
            try:
                self._revalidate_sources(scope, definition, data, now)
                if self.evidence_reviews is not None:
                    self.evidence_reviews.verification_evidence(scope, action.record_id, data, at=now)
            except PermissionError:
                sources_current = False
            else:
                sources_current = True
            if data.get("verified_evidence_digest") != current_digest or not sources_current:
                self.verification.record_failure(
                    scope.tenant_id,
                    scope.company_id,
                    verification_id,
                    "Founder-action evidence changed after verification",
                    at=now,
                )
                self._invalidate_action(action, current_digest)
                raise PermissionError("verified evidence changed")
            return self._result(action.record_id, verification_id)
        if current is not FounderActionStage.RESULT_CAPTURED:
            raise PermissionError("result must be captured before verification")

        self._revalidate_sources(scope, definition, data, now)
        review_evidence = (
            self.evidence_reviews.verification_evidence(scope, action.record_id, data, at=now)
            if self.evidence_reviews is not None
            else ()
        )
        test_evidence = self.verifier.review(data, at=now)
        test_evidence = EvidenceRef(
            **{
                **test_evidence.__dict__,
                "tenant_id": scope.tenant_id,
                "company_id": scope.company_id,
            }
        )
        evidence = tuple(self._evidence_ref(scope, item) for item in data["evidence"]) + review_evidence
        try:
            verification = self.verification.get(
                scope.tenant_id, scope.company_id, verification_id
            )
        except KeyError:
            verification = self.verification.create(
                VerificationRecord(
                    verification_id=verification_id,
                    tenant_id=scope.tenant_id,
                    company_id=scope.company_id,
                    definition_id=definition.verification_definition_id,
                    target_type="founder_action",
                    target_id=action.record_id,
                    state=VerificationState.PROPOSED,
                    owner=self.verifier.verifier_id,
                    scope=definition.title,
                    method=VerificationMethod.AUTOMATED_PLUS_HUMAN,
                    provenance={"source": "residential_cleaning_pilot", "job_id": request.job_id},
                    created_at=now,
                    updated_at=now,
                )
            )
        if verification.state is VerificationState.PROPOSED:
            verification = self.verification.transition(
                scope.tenant_id, scope.company_id, verification_id,
                VerificationState.EXECUTED, evidence=evidence, at=now,
            )
        if verification.state is VerificationState.EXECUTED:
            verification = self.verification.transition(
                scope.tenant_id, scope.company_id, verification_id,
                VerificationState.TESTED, evidence=(test_evidence,), at=now,
            )
        if verification.state is VerificationState.TESTED:
            verification = self.verification.transition(
                scope.tenant_id, scope.company_id, verification_id,
                VerificationState.VERIFIED, at=now,
            )
        if verification.state is not VerificationState.VERIFIED:
            raise PermissionError("Verification did not accept the founder action")
        stamp = _iso(now)
        changed = {
            **data,
            "state": FounderActionStage.VERIFIED.value,
            "verified_evidence_digest": current_digest,
            "verification": {
                "authority": "verification",
                "state": verification.state.value,
                "verification_id": verification.verification_id,
                "result": "accepted",
            },
            "timestamps": {**data["timestamps"], FounderActionStage.VERIFIED.value: stamp},
            "last_actor": {"type": "verification", "id": self.verifier.verifier_id},
            "history": [*data["history"], {
                "state": FounderActionStage.VERIFIED.value,
                "at": stamp,
                "actor_type": "verification",
                "actor_id": self.verifier.verifier_id,
                "reason": "Verification accepted the complete scoped evidence set",
            }],
        }
        self.company_brain.update_approved_state(
            scope,
            record_id=action.record_id,
            kind=action.kind,
            data=changed,
            knowledge_class=KnowledgeClass.EXTERNAL_VERIFICATION,
            provenance=action.provenance,
            confidence=None,
            owner_ref=action.owner_ref,
            expected_version=action.version,
        )
        return self._result(action.record_id, verification_id)

    def status(self, provider_ref: str) -> str:
        return "completed" if provider_ref.startswith("verification:founder-action:") else "unknown"

    def cancel(self, provider_ref: str) -> bool:
        return False

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        return None

    @staticmethod
    def _evidence_ref(scope: Scope, item: dict[str, Any]) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=item["evidence_id"],
            evidence_type=EvidenceType(item["evidence_type"]),
            artifact_ref=item["reference"],
            tenant_id=scope.tenant_id,
            company_id=scope.company_id,
            captured_at=_parse_time(item["captured_at"]),
            issuer=item.get("issuer"),
            provenance={"content_digest": item["content_digest"], "source_class": item["source_class"]},
        )

    def _revalidate_sources(
        self,
        scope: Scope,
        definition: FounderActionDefinition,
        data: dict[str, Any],
        at: datetime,
    ) -> None:
        owner_id = self.company_brain.get_company(scope).owner_refs[0].id
        for item in data["evidence"]:
            if item["source_class"] == "founder_assertion":
                if item.get("issuer") != owner_id:
                    raise PermissionError("founder assertion ownership changed")
            elif item["source_class"] == "artifact":
                artifact = self.runtime_repository.get_broker_record(
                    "artifact", scope.tenant_id, scope.company_id, item["reference"]
                )
                if (
                    artifact is None
                    or artifact.status is not ArtifactStatus.AVAILABLE
                    or (artifact.expires_at is not None and artifact.expires_at <= at)
                    or item["content_digest"] != f"sha256:{artifact.content_sha256}"
                ):
                    raise PermissionError("evidence artifact is no longer current")
                if item["evidence_type"] == EvidenceType.AUTHORITY_CONFIRMATION.value and (
                    artifact.provenance_ref
                    != f"verified_authority:{definition.authority_code}"
                ):
                    raise PermissionError("authority evidence provenance changed")
            elif item["source_class"] == "provider_receipt":
                receipt = next(
                    (
                        candidate
                        for candidate in self.runtime_repository.list_provider_receipts(
                            scope.tenant_id, scope.company_id
                        )
                        if candidate.receipt_id == item["reference"]
                    ),
                    None,
                )
                expected_digest = (
                    _digest(receipt.receipt_id, receipt.provider_request_id, receipt.status.value)
                    if receipt is not None
                    else None
                )
                if (
                    receipt is None
                    or receipt.status is not ReceiptStatus.SUCCEEDED
                    or receipt.capability != EVIDENCE_CAPABILITY
                    or receipt.operation != definition.provider_operation
                    or item["content_digest"] != expected_digest
                ):
                    raise PermissionError("provider receipt is no longer authoritative")
            else:
                raise PermissionError("unknown evidence source class")

    def _invalidate_action(self, action, current_digest: str) -> None:
        changed = {
            **dict(action.data),
            "state": FounderActionStage.RESULT_CAPTURED.value,
            "verification": {
                **dict(action.data["verification"]),
                "state": "invalidated",
                "result": "evidence_changed",
            },
            "verified_evidence_digest": None,
            "history": [*action.data["history"], {
                "state": FounderActionStage.RESULT_CAPTURED.value,
                "at": _iso(self.clock()),
                "actor_type": "verification",
                "actor_id": self.verifier.verifier_id,
                "reason": f"Evidence digest changed; current digest {current_digest}",
            }],
        }
        self.company_brain.update_approved_state(
            action.scope,
            record_id=action.record_id,
            kind=action.kind,
            data=changed,
            knowledge_class=KnowledgeClass.FACT,
            provenance=action.provenance,
            confidence=None,
            owner_ref=action.owner_ref,
            expected_version=action.version,
        )

    @staticmethod
    def _result(action_id: str, verification_id: str) -> CapabilityResult:
        return CapabilityResult(
            provider_ref=f"verification:founder-action:{verification_id}",
            status="completed",
            artifacts=(ArtifactRef("pilot_founder_action_verification", action_id),),
            spend=Money("USD", 0),
            progress=("founder_action_verified",),
        )


def evidence_digest(evidence: object) -> str:
    safe = [
        {
            "evidence_id": item["evidence_id"],
            "evidence_type": item["evidence_type"],
            "reference": item["reference"],
            "content_digest": item["content_digest"],
            "issuer": item.get("issuer"),
        }
        for item in evidence
    ] if isinstance(evidence, list) else []
    return _digest(json.dumps(safe, sort_keys=True, separators=(",", ":")))


def _digest(*parts: str) -> str:
    return "sha256:" + sha256(":".join(parts).encode()).hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
