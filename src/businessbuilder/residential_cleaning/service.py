from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any, Callable

from businessbuilder.commercial import (
    CommercialService,
    EntitlementStatus,
    NormalizedBillingEvent,
    OrderStatus,
    ProductCode,
)
from businessbuilder.commercial.repository import CommercialNotFound, CommercialRepository
from businessbuilder.commercial.pricing import OfferCode
from businessbuilder.commercial.models import PaymentEligibility, TaxDisposition
from businessbuilder.commercial.operator_authority import CommercialOperatorPrincipal
from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    KnowledgeClass,
    LifecycleState,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.company_brain.errors import NotFoundError
from businessbuilder.identity import (
    AuthenticatedPrincipal,
    AuthorizationContext,
    IdentityService,
    MembershipStatus,
    OrganizationStatus,
    PrincipalContextAuthority,
    Role,
    User,
)
from businessbuilder.identity.repository import IdentityRepository
from businessbuilder.runtime import ApprovalMode, ApprovalState, Budget, JobStatus, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.verification import EvidenceType, VerificationService
from businessbuilder.access_broker.ports import ArtifactStorePort

from .capability import ResidentialCleaningScopeCommitCapability, canonical_digest
from .founder_actions import (
    DeterministicResidentialCleaningEvidenceVerifier,
    DEFINITIONS_BY_ID,
    FounderActionStage,
    ResidentialCleaningReviewedEvidenceVerifier,
    ResidentialCleaningFounderActionTransitionCapability,
    ResidentialCleaningFounderActionVerificationCapability,
    TRANSITION_CAPABILITY,
    VERIFY_CAPABILITY,
    register_verification_definitions,
)
from .research import SUPPORTED_JURISDICTION, recommendation, research_packet
from .evidence_review import (
    MalwareScannerPort,
    ResidentialCleaningEvidenceReviewService,
    ReviewDecision,
)


SUPPORTED_RESPONSIBILITIES = frozenset(
    {"BUSINESS_BUILDER", "FOUNDER_ACTION", "EXTERNAL_PROVIDER/AUTHORITY"}
)
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,160}$")


class PilotConflict(RuntimeError):
    pass


class PilotNotFound(LookupError):
    pass


class ResidentialCleaningJourneyService:
    """Composes existing authorities for the one Denton cleaning pilot."""

    def __init__(
        self,
        *,
        identity_repository: IdentityRepository,
        principal_authority: PrincipalContextAuthority,
        company_brain: CompanyBrainService,
        runtime: JobOrchestrator,
        runtime_repository: RuntimeRepository,
        commercial: CommercialService,
        commercial_repository: CommercialRepository,
        verification: VerificationService,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
        enable_test_checkout: bool = False,
        evidence_verifier: DeterministicResidentialCleaningEvidenceVerifier | None = None,
        evidence_store: ArtifactStorePort | None = None,
        malware_scanner: MalwareScannerPort | None = None,
    ) -> None:
        self.identity_repository = identity_repository
        self.principal_authority = principal_authority
        self.company_brain = company_brain
        self.runtime = runtime
        self.runtime_repository = runtime_repository
        self.commercial = commercial
        self.commercial_repository = commercial_repository
        self.verification = verification
        self.id_factory = id_factory
        self.clock = clock
        self.enable_test_checkout = enable_test_checkout
        self.evidence_reviews = (
            ResidentialCleaningEvidenceReviewService(
                repository=runtime_repository,
                artifact_store=evidence_store,
                principal_authority=principal_authority,
                scanner=malware_scanner,
                clock=clock,
                id_factory=id_factory,
            )
            if evidence_store is not None
            else None
        )
        self.evidence_verifier = evidence_verifier or (
            ResidentialCleaningReviewedEvidenceVerifier()
            if self.evidence_reviews is not None
            else None
        )
        register_verification_definitions(verification)
        registered = set(runtime.registry.list())
        if (ResidentialCleaningScopeCommitCapability.name, "v1") not in registered:
            runtime.registry.register(ResidentialCleaningScopeCommitCapability(company_brain))
        if (TRANSITION_CAPABILITY, "v1") not in registered:
            runtime.registry.register(
                ResidentialCleaningFounderActionTransitionCapability(
                    company_brain, runtime_repository, clock, self.evidence_reviews
                )
            )
        if self.evidence_verifier is not None and (VERIFY_CAPABILITY, "v1") not in registered:
            runtime.registry.register(
                ResidentialCleaningFounderActionVerificationCapability(
                    company_brain,
                    verification,
                    self.evidence_verifier,
                    runtime_repository,
                    clock,
                    self.evidence_reviews,
                )
            )

    def submit_evidence_upload(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        evidence_type: str,
        filename: str,
        content_type: str,
        content: bytes,
        idempotency_key: str,
        supersedes_submission_id: str | None = None,
    ) -> dict[str, Any]:
        if self.evidence_reviews is None:
            raise PermissionError("customer evidence storage is not configured")
        verified = self._require_founder(principal)
        action = self._record(Scope(verified.tenant_id, verified.company_id or ""), action_id)
        if FounderActionStage(action.data["state"]) not in {
            FounderActionStage.FOUNDER_COMPLETED,
            FounderActionStage.RESULT_CAPTURED,
            FounderActionStage.VERIFIED,
        }:
            raise PermissionError("founder must complete the prepared action before submitting evidence")
        submission = self.evidence_reviews.submit_upload(
            verified,
            action_id=action_id,
            evidence_type=evidence_type,
            filename=filename,
            content_type=content_type,
            content=content,
            idempotency_key=idempotency_key,
            supersedes_submission_id=supersedes_submission_id,
        )
        return self.evidence_reviews.public_submission(submission)

    def submit_evidence_reference(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        evidence_type: str,
        source: str,
        reference: dict[str, Any],
        idempotency_key: str,
        supersedes_submission_id: str | None = None,
    ) -> dict[str, Any]:
        if self.evidence_reviews is None:
            raise PermissionError("customer evidence storage is not configured")
        verified = self._require_founder(principal)
        action = self._record(Scope(verified.tenant_id, verified.company_id or ""), action_id)
        if FounderActionStage(action.data["state"]) not in {
            FounderActionStage.FOUNDER_COMPLETED,
            FounderActionStage.RESULT_CAPTURED,
            FounderActionStage.VERIFIED,
        }:
            raise PermissionError("founder must complete the prepared action before submitting evidence")
        submission = self.evidence_reviews.submit_reference(
            verified,
            action_id=action_id,
            evidence_type=evidence_type,
            source=source,
            reference=reference,
            idempotency_key=idempotency_key,
            supersedes_submission_id=supersedes_submission_id,
        )
        return self.evidence_reviews.public_submission(submission)

    def review_evidence(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        submission_ids: list[str],
        decision: str,
        reason_code: str,
        idempotency_key: str,
        operator_notes: str | None = None,
        requested_additional_evidence: list[str] | None = None,
        supersedes_review_id: str | None = None,
    ) -> dict[str, Any]:
        if self.evidence_reviews is None:
            raise PermissionError("operator evidence review is not configured")
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        review = self.evidence_reviews.review(
            verified,
            action_id=action_id,
            submission_ids=submission_ids,
            decision=decision,
            reason_code=reason_code,
            idempotency_key=idempotency_key,
            operator_notes=operator_notes,
            requested_additional_evidence=requested_additional_evidence,
            supersedes_review_id=supersedes_review_id,
        )
        scope = Scope(verified.tenant_id, verified.company_id or "")
        action = self._record(scope, action_id)
        if review.decision is ReviewDecision.ACCEPTED and FounderActionStage(action.data["state"]) is FounderActionStage.FOUNDER_COMPLETED:
            definition = DEFINITIONS_BY_ID[action_id]
            required = set(definition.required_evidence) - {
                EvidenceType.FOUNDER_ATTESTATION,
                EvidenceType.TEST_RESULT,
            }
            available = set(self.evidence_reviews.accepted_evidence_types(
                scope.tenant_id, scope.company_id, action_id
            ))
            if required.issubset(available):
                self._capture_reviewed_evidence(
                    scope,
                    action_id,
                    verified.user_id,
                    idempotency_key,
                )
                action = self._record(scope, action_id)
        if (
            self.evidence_verifier is not None
            and FounderActionStage(action.data["state"]) in {
                FounderActionStage.RESULT_CAPTURED,
                FounderActionStage.VERIFIED,
            }
        ):
            try:
                self.verify_founder_action_internal(
                    tenant_id=scope.tenant_id,
                    company_id=scope.company_id,
                    action_id=action_id,
                    idempotency_key=(
                        "operator-review-verification:"
                        + self._suffix(action_id, review.review_id, review.request_digest)
                    ),
                )
            except (PermissionError, PilotConflict):
                pass
        current = self._record(scope, action_id)
        return {
            "review": self.evidence_reviews.public_review(review, True),
            "founder_action": self._public_founder_action_with_evidence(current),
        }

    def issue_evidence_access(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        submission_id: str,
    ) -> dict[str, Any]:
        if self.evidence_reviews is None:
            raise PermissionError("customer evidence storage is not configured")
        return self.evidence_reviews.issue_download(
            principal, action_id=action_id, submission_id=submission_id
        )

    def evidence_state(self, principal: AuthenticatedPrincipal, *, action_id: str) -> dict[str, Any]:
        if self.evidence_reviews is None:
            return {"submissions": [], "reviews": [], "review_state": "pending_review"}
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        self._record(Scope(verified.tenant_id, verified.company_id or ""), action_id)
        return self.evidence_reviews.public_action_state(
            verified.tenant_id, verified.company_id or "", action_id
        )

    def _capture_reviewed_evidence(
        self,
        scope: Scope,
        action_id: str,
        operator_id: str,
        idempotency_key: str,
    ) -> None:
        assert self.evidence_reviews is not None
        evidence_refs, review_ids = self.evidence_reviews.active_accepted_references(
            scope.tenant_id, scope.company_id, action_id
        )
        inputs = {
            "objective": "Capture current operator-reviewed residential-cleaning evidence",
            "operation": "capture_reviewed_result",
            "action_id": action_id,
            "actor_id": operator_id,
            "evidence_refs": evidence_refs,
            "review_ids": list(review_ids),
        }
        job_key = f"cleaning-reviewed-evidence:{idempotency_key}"
        existing = self.runtime_repository.get_job_by_idempotency(
            scope.tenant_id, scope.company_id, job_key
        )
        if existing is not None and existing.inputs != inputs:
            raise PilotConflict("review idempotency key was already used for different evidence")
        budget_id = f"budget_review_{self._suffix(scope.company_id, idempotency_key)}"
        if self.runtime_repository.get_budget(scope.tenant_id, scope.company_id, budget_id) is None:
            self.runtime.budgets.create(
                Budget(budget_id, scope.tenant_id, scope.company_id, Money("USD", 0)),
                f"correlation_{self._suffix(idempotency_key)}",
            )
        job = self.runtime.create_job(
            tenant_id=scope.tenant_id,
            company_id=scope.company_id,
            capability=TRANSITION_CAPABILITY,
            inputs=inputs,
            budget_ref=budget_id,
            per_job_ceiling=Money("USD", 0),
            idempotency_key=job_key,
            correlation_id=f"correlation_{self._suffix(idempotency_key)}",
            approval_mode=ApprovalMode.AUTONOMOUS,
            job_id=f"job_review_{self._suffix(scope.company_id, idempotency_key)}",
            provenance={"source": "authorized_operator_review", "action_id": action_id},
        )
        if job.status is not JobStatus.SUCCEEDED:
            job = self.runtime.run(scope.tenant_id, scope.company_id, job.job_id)
        if job.status is not JobStatus.SUCCEEDED:
            raise PilotConflict("Runtime did not capture the reviewed evidence")

    def start(
        self,
        *,
        raw_session_token: str,
        user: User,
        intake: dict[str, Any],
        idempotency_key: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        normalized = self._validate_intake(intake)
        if not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise ValueError("a canonical idempotency key of 16 through 160 characters is required")
        # Re-authenticate at the composition boundary. The user object is only a
        # convenience passed by the transport and is never trusted on its own.
        authenticated = self.principal_authority.authenticate_session(raw_session_token)
        if authenticated.user_id != user.user_id:
            raise PermissionError("authenticated user mismatch")

        identity = IdentityService(
            self.identity_repository, id_factory=self.id_factory, clock=self.clock
        )
        if self.identity_repository.get_founder_profile_for_user(user.user_id) is None:
            identity.create_founder_profile(user.user_id, normalized["founder_display_name"])

        active = tuple(
            item
            for item in self.identity_repository.list_user_memberships(user.user_id)
            if item.status is MembershipStatus.ACTIVE
        )
        if not active:
            suffix = self._suffix(user.user_id, idempotency_key)
            tenant, organization, membership = identity.create_account(
                user.user_id,
                normalized["organization_name"],
                tenant_id=f"tenant_cleaning_{suffix}",
                organization_id=f"org_cleaning_{suffix}",
                membership_id=f"membership_cleaning_owner_{suffix}",
            )
        elif len(active) == 1 and active[0].role is Role.OWNER:
            membership = active[0]
            tenant = self.identity_repository.get_tenant(membership.tenant_id)
            organization = self.identity_repository.get_organization(membership.organization_id)
            if organization.status is not OrganizationStatus.ACTIVE:
                raise PermissionError("organization is not active")
        else:
            raise PermissionError("pilot start requires one unambiguous founder-owned organization")

        suffix = self._suffix(tenant.tenant_id, user.user_id, idempotency_key)
        company_id = f"company_cleaning_{suffix}"
        scope = Scope(tenant.tenant_id, company_id)
        owner = EntityRef("user", user.user_id)
        captured = self._iso(self.clock())
        provenance = (
            Provenance(
                "authenticated_founder_intake",
                captured,
                owner,
                source_ref=f"customer-api://residential-cleaning/{company_id}",
            ),
        )

        try:
            company = self.company_brain.get_company(scope)
        except NotFoundError:
            company = self.company_brain.create_company(
                Company(
                    scope,
                    normalized["company_name"],
                    "residential_cleaning",
                    dict(SUPPORTED_JURISDICTION),
                    (owner,),
                    lifecycle=LifecycleState.DRAFT,
                    provenance=provenance,
                    permissions=("founder.approve", "founder.export", "founder.revoke_access"),
                )
            )
        if company.owner_refs != (owner,) or company.archetype != "residential_cleaning":
            raise PilotConflict("idempotency key resolves to a conflicting company")
        captured = company.created_at
        provenance = (
            Provenance(
                "authenticated_founder_intake",
                captured,
                owner,
                source_ref=f"customer-api://residential-cleaning/{company_id}",
            ),
        )
        if company_id not in organization.company_ids:
            identity.attach_company(
                AuthorizationContext(user.user_id, tenant.tenant_id), company_id
            )

        intake_id = "intake_residential_cleaning_v1"
        intake_data = {
            **normalized,
            "intake_version": "residential-cleaning.v1",
            "responsibility": "FOUNDER_ACTION",
            "state": "captured",
            "content_digest": canonical_digest(normalized),
        }
        self._ensure_record(
            scope, intake_id, RecordKind.GOAL, intake_data, KnowledgeClass.FACT,
            provenance, owner,
        )

        packet_id = "research_residential_cleaning_denton_v1"
        packet = research_packet(captured_at=self._parse_time(captured))
        research_provenance = tuple(
            Provenance(
                "authoritative_public_source",
                captured,
                EntityRef("publisher", source["source_id"]),
                source_ref=source["url"],
            )
            for source in packet["sources"]
        )
        self._ensure_record(
            scope, packet_id, RecordKind.MARKET, packet, KnowledgeClass.FACT,
            research_provenance, owner,
        )

        recommendation_id = "recommendation_residential_cleaning_v1"
        proposed = recommendation(intake=intake_data, research_record_id=packet_id)
        proposed["responsibility"] = "BUSINESS_BUILDER"
        self._validate_responsibilities(proposed)
        self._ensure_record(
            scope, recommendation_id, RecordKind.STRATEGY, proposed,
            KnowledgeClass.INFERENCE, provenance + research_provenance, owner,
            confidence=0.55,
        )
        recommendation_digest = canonical_digest(proposed)

        company = self.company_brain.get_company(scope)
        if company.lifecycle is LifecycleState.DRAFT:
            self.company_brain.transition_company(
                scope, LifecycleState.CHALLENGED, expected_version=company.version
            )
        elif company.lifecycle not in {
            LifecycleState.CHALLENGED,
            LifecycleState.APPROVED,
            LifecycleState.ASSEMBLY,
        }:
            raise PilotConflict("company is outside the pilot recommendation lifecycle")

        # Once created, the workflow is the durable composition checkpoint.
        # Later retries return its current projection instead of attempting to
        # recreate mutable approval/commercial state.
        try:
            existing_workflow = self.company_brain.repository.get_record(
                scope, "pilot_residential_cleaning_v1"
            )
        except NotFoundError:
            existing_workflow = None
        if existing_workflow is not None:
            expected = {
                "intake_id": intake_id,
                "research_record_id": packet_id,
                "recommendation_id": recommendation_id,
                "recommendation_digest": recommendation_digest,
            }
            if any(existing_workflow.data.get(key) != value for key, value in expected.items()):
                raise PilotConflict("existing pilot workflow conflicts with intake")
            principal = self.principal_authority.issue(
                raw_session_token, tenant_id=tenant.tenant_id, company_id=company_id
            )
            return self.project(principal)

        budget_id = f"budget_cleaning_scope_{suffix}"
        if self.runtime_repository.get_budget(tenant.tenant_id, company_id, budget_id) is None:
            self.runtime.budgets.create(
                Budget(budget_id, tenant.tenant_id, company_id, Money("USD", 0)),
                correlation_id,
            )
        job = self.runtime.create_job(
            tenant_id=tenant.tenant_id,
            company_id=company_id,
            capability=ResidentialCleaningScopeCommitCapability.name,
            inputs={
                "vertical": "residential_cleaning",
                "objective": "Commit the founder-approved residential-cleaning pilot scope",
                "recommendation_id": recommendation_id,
                "recommendation_digest": recommendation_digest,
                "research_record_id": packet_id,
            },
            budget_ref=budget_id,
            per_job_ceiling=Money("USD", 0),
            idempotency_key=f"cleaning-scope:{suffix}",
            correlation_id=correlation_id,
            approval_mode=ApprovalMode.FOUNDER_ONLY,
            job_id=f"job_cleaning_scope_{suffix}",
            provenance={
                "source": "authenticated_founder_intake",
                "intake_id": intake_id,
                "recommendation_id": recommendation_id,
            },
        )
        approval_id = job.approval_ids[0]
        self._ensure_record(
            scope,
            "founder_action_approve_cleaning_scope",
            RecordKind.FOUNDER_ACTION,
            {
                "title": "Approve the residential cleaning build scope",
                "reason": "The proposed customer, service area, offers, price logic, positioning, risks, requirements, and systems become canonical only after approval.",
                "instructions": ["Review the cited research packet.", "Review the complete recommendation.", "Approve the exact Runtime request or request changes."],
                "risk": "high",
                "irreversible": False,
                "state": "required" if job.status is JobStatus.WAITING_APPROVAL else "approved",
                "selected": True,
                "critical": True,
                "responsibility": "FOUNDER_ACTION",
                "partner_authority": None,
                "required_evidence_kinds": ["runtime_founder_approval"],
                "approval_id": approval_id,
            },
            KnowledgeClass.FACT,
            provenance,
            owner,
        )
        self._ensure_record(
            scope,
            "pilot_residential_cleaning_v1",
            RecordKind.WORKFLOW,
            {
                "pilot": "residential_cleaning_denton_v1",
                "state": "awaiting_founder_approval",
                "intake_id": intake_id,
                "research_record_id": packet_id,
                "recommendation_id": recommendation_id,
                "recommendation_digest": recommendation_digest,
                "job_id": job.job_id,
                "approval_id": approval_id,
                "responsibility": "BUSINESS_BUILDER",
            },
            KnowledgeClass.FACT,
            provenance,
            owner,
        )
        principal = self.principal_authority.issue(
            raw_session_token, tenant_id=tenant.tenant_id, company_id=company_id
        )
        return self.project(principal)

    def approve(
        self,
        principal: AuthenticatedPrincipal,
        *,
        approval_id: str,
    ) -> dict[str, Any]:
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        scope = Scope(verified.tenant_id, verified.company_id or "")
        workflow = self._record(scope, "pilot_residential_cleaning_v1")
        if workflow.data.get("approval_id") != approval_id:
            raise PermissionError("approval does not belong to this pilot")
        approval = self.runtime_repository.get_approval(
            verified.tenant_id, verified.company_id or "", approval_id
        )
        if approval is None:
            raise PilotNotFound("pilot approval not found")
        if approval.state is ApprovalState.REQUESTED:
            job = self.runtime.approve_job(
                tenant_id=verified.tenant_id,
                company_id=verified.company_id or "",
                job_id=workflow.data["job_id"],
                approval_id=approval_id,
                principal=verified,
            )
        elif (
            approval.state is ApprovalState.GRANTED
            and approval.decided_by == verified.user_id
            and approval.decided_by_role == "founder"
        ):
            job = self.runtime_repository.get_job(
                verified.tenant_id, verified.company_id or "", workflow.data["job_id"]
            )
            if job is None:
                raise PilotNotFound("pilot scope job not found")
        else:
            raise PermissionError("pilot approval is not an effective founder approval")
        if job.status is not JobStatus.SUCCEEDED:
            job = self.runtime.run(verified.tenant_id, verified.company_id or "", job.job_id)
        if job.status is not JobStatus.SUCCEEDED:
            raise PilotConflict("approved pilot scope did not complete")

        self._mark_scope_approval_complete(scope, verified.user_id, approval_id)

        intake = self._record(scope, "intake_residential_cleaning_v1")
        if intake.data.get("starting_point") == "running":
            # The Existing Business + Run offer is a starting price, not a fixed
            # new-business charge. Do not create an order before an audit/quote.
            updated_workflow = {
                **dict(workflow.data),
                "state": "existing_business_audit_required",
                "order_id": None,
                "order_status": None,
                "entitlement_ids": [],
                "commercial_mode": "audit_quote_founder_approval_required",
            }
            if dict(workflow.data) != updated_workflow:
                self.company_brain.update_approved_state(
                    scope, record_id=workflow.record_id, kind=workflow.kind,
                    data=updated_workflow, knowledge_class=workflow.knowledge_class,
                    provenance=workflow.provenance, confidence=workflow.confidence,
                    owner_ref=workflow.owner_ref, expected_version=workflow.version,
                )
            return self.project(verified)

        suffix = verified.company_id.removeprefix("company_cleaning_")
        order_id = f"order_cleaning_build_{suffix}"
        context = AuthorizationContext(
            verified.user_id, verified.tenant_id, verified.company_id
        )
        try:
            order = self.commercial_repository.get_order(
                verified.tenant_id, verified.company_id or "", order_id
            )
        except CommercialNotFound:
            order = self.commercial.create_order(
                context,
                f"product_version_{ProductCode.BUILD_BUSINESS.value.lower()}_v1",
                order_id=order_id,
                offer_code=OfferCode.BUSINESS,
            )
        if order.offer_code is None and order.status is OrderStatus.DRAFT:
            order = self.commercial.select_fixed_offer(context, order.order_id, OfferCode.BUSINESS)
        if self.enable_test_checkout:
            if order.eligibility is PaymentEligibility.PAYMENT_DELAY_REQUIRED:
                order = self.commercial.record_payment_readiness(
                    tenant_id=order.tenant_id, company_id=order.company_id,
                    order_id=order.order_id,
                    eligibility=PaymentEligibility.PAY_NOW_ELIGIBLE,
                    eligible_at=None,
                    tax_disposition=TaxDisposition.NON_TAXABLE,
                    review_ref="deterministic_test_only_tax_and_eligibility",
                )
            order = self._activate_test_order(context, order)

        grants = tuple(
            item
            for item in self.commercial_repository.get_current_entitlement_grants(
                verified.tenant_id, verified.company_id or ""
            )
            if item.source_order_id == order_id and item.status is EntitlementStatus.ACTIVE
        )
        updated_workflow = {
            **dict(workflow.data),
            "state": "scope_committed",
            "order_id": order_id,
            "order_status": order.status.value,
            "entitlement_ids": [item.grant_id for item in grants],
            "commercial_mode": (
                "deterministic_test_no_money_movement"
                if self.enable_test_checkout
                else "awaiting_authoritative_billing_event"
            ),
        }
        if dict(workflow.data) != updated_workflow:
            self.company_brain.update_approved_state(
                scope,
                record_id=workflow.record_id,
                kind=workflow.kind,
                data=updated_workflow,
                knowledge_class=workflow.knowledge_class,
                provenance=workflow.provenance,
                confidence=workflow.confidence,
                owner_ref=workflow.owner_ref,
                expected_version=workflow.version,
            )
        return self.project(verified)

    def capture_existing_business_audit(
        self, principal: AuthenticatedPrincipal, *, systems: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record founder-supplied cleaning-system facts, not an operator-approved quote."""
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        if verified.role is not Role.OWNER or verified.company_id is None:
            raise PermissionError("only the scoped founder may submit this audit inventory")
        scope = Scope(verified.tenant_id, verified.company_id)
        intake = self._record(scope, "intake_residential_cleaning_v1")
        if intake.data.get("starting_point") != "running":
            raise PilotConflict("existing-business audit belongs only to the running-business path")
        if not isinstance(systems, list) or not 1 <= len(systems) <= 12:
            raise ValueError("one through twelve existing systems are required")
        allowed_systems = {
            "website", "lead_capture", "crm", "inbox", "scheduling", "payments",
            "bookkeeping", "local_presence", "business_phone", "analytics",
        }
        inventory = []
        seen = set()
        for raw in systems:
            if not isinstance(raw, dict) or set(raw) - {"system", "assessment", "issue", "provider_reference"}:
                raise ValueError("existing system record is invalid")
            system, assessment, issue = raw.get("system"), raw.get("assessment"), raw.get("issue")
            provider_reference = raw.get("provider_reference")
            if system not in allowed_systems or system in seen or assessment not in {"keep", "improve", "replace", "missing"}:
                raise ValueError("existing system classification is invalid")
            if not isinstance(issue, str) or not 3 <= len(issue.strip()) <= 500:
                raise ValueError("existing system issue is invalid")
            if provider_reference is not None and (
                not isinstance(provider_reference, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{3,160}", provider_reference)
            ):
                raise ValueError("provider reference must be opaque")
            seen.add(system)
            inventory.append({"system": system, "assessment": assessment,
                              "issue": issue.strip(), "provider_reference": provider_reference})
        packet = {
            "vertical": "residential_cleaning",
            "state": "founder_inventory_pending_operator_scope",
            "systems": inventory,
            "responsibility": "FOUNDER_ACTION",
            "source_class": "founder_assertion_not_verified",
            "content_digest": canonical_digest(inventory),
        }
        owner = EntityRef("user", verified.user_id)
        provenance = (Provenance(
            "authenticated_founder_existing_systems", self._iso(self.clock()), owner,
            source_ref=f"customer-api://residential-cleaning/{verified.company_id}/existing-audit",
        ),)
        record = self._ensure_record(
            scope, "existing_business_audit_residential_cleaning_v1",
            RecordKind.WORKFLOW, packet, KnowledgeClass.FACT, provenance, owner,
        )
        return self._public_record(record)

    def publish_existing_business_scope(
        self, operator: CommercialOperatorPrincipal, *, tenant_id: str,
        company_id: str, findings: list[dict[str, str]],
        citation_ids: list[str],
    ) -> dict[str, Any]:
        """Operator-authored, cited assessment; never a founder assertion or charge."""
        authority = self.commercial.operator_authority
        if authority is None:
            raise PermissionError("commercial operator authority unavailable")
        verified = authority.verify(
            operator, tenant_id=tenant_id, company_id=company_id,
            action="existing_scope.publish",
        )
        scope = Scope(tenant_id, company_id)
        intake = self._record(scope, "intake_residential_cleaning_v1")
        if intake.data.get("starting_point") != "running":
            raise PilotConflict("scoped audit only belongs to the existing-business path")
        inventory = self._record(scope, "existing_business_audit_residential_cleaning_v1")
        research = self._record(scope, "research_residential_cleaning_denton_v1")
        available = {item["source_id"] for item in research.data["sources"]}
        if not citation_ids or len(citation_ids) > 8 or not set(citation_ids) <= available:
            raise ValueError("operator assessment requires known research citations")
        founder_systems = {item["system"] for item in inventory.data["systems"]}
        if not isinstance(findings, list) or not 1 <= len(findings) <= 12:
            raise ValueError("one through twelve scoped findings required")
        normalized = []
        seen = set()
        for raw in findings:
            if not isinstance(raw, dict) or set(raw) != {"system", "decision", "reason"}:
                raise ValueError("scoped finding is invalid")
            system, decision, reason = raw["system"], raw["decision"], raw["reason"]
            if (system not in founder_systems or system in seen
                or decision not in {"keep", "improve", "replace", "add"}
                or not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 500):
                raise ValueError("scoped finding must be bounded and inventory-backed")
            seen.add(system)
            normalized.append({"system": system, "decision": decision, "reason": reason.strip()})
        packet = {
            "vertical": "residential_cleaning", "state": "operator_scoped_pending_founder_quote_approval",
            "inventory_record_id": inventory.record_id,
            "inventory_digest": inventory.data["content_digest"],
            "research_record_id": research.record_id,
            "citation_ids": sorted(set(citation_ids)),
            "findings": normalized,
            "responsibility": "BUSINESS_BUILDER",
            "scope_version": "existing_business_cleaning.v1",
        }
        owner = EntityRef("user", verified.operator_user_id)
        provenance = (Provenance(
            "appointed_operator_existing_scope", self._iso(self.clock()), owner,
            source_ref=f"operator://existing-scope/{company_id}",
        ),)
        record = self._ensure_record(
            scope, "existing_business_scope_residential_cleaning_v1",
            RecordKind.STRATEGY, packet, KnowledgeClass.INFERENCE,
            provenance, owner,
        )
        return self._public_record(record)

    def create_existing_business_order(
        self, principal: AuthenticatedPrincipal,
    ) -> dict[str, Any]:
        """Founder starts an unpriced order from a real operator-reviewed scope."""
        verified = self._require_founder(principal)
        scope = Scope(verified.tenant_id, verified.company_id or "")
        workflow = self._record(scope, "pilot_residential_cleaning_v1")
        approval = self.runtime_repository.get_approval(
            scope.tenant_id, scope.company_id, workflow.data["approval_id"]
        )
        if approval is None or approval.state is not ApprovalState.GRANTED:
            raise PilotConflict("initial Runtime scope approval required")
        scoped = self._record(scope, "existing_business_scope_residential_cleaning_v1")
        suffix = verified.company_id.removeprefix("company_cleaning_")
        order_id = f"order_cleaning_existing_{suffix}"
        digest = canonical_digest(dict(scoped.data)).removeprefix("sha256:")
        try:
            order = self.commercial_repository.get_order(scope.tenant_id, scope.company_id, order_id)
        except CommercialNotFound:
            order = self.commercial.create_existing_business_order(
                AuthorizationContext(verified.user_id, scope.tenant_id, scope.company_id),
                audit_ref=scoped.record_id, recommendation_digest=digest,
                order_id=order_id,
            )
        if (order.user_id != verified.user_id or order.existing_audit_ref != scoped.record_id
            or order.existing_recommendation_digest != digest):
            raise PilotConflict("existing order does not match current operator scope")
        return {"order_id": order.order_id, "status": order.status.value,
                "quote_id": order.quote_id, "priced": order.total is not None}

    def transition_founder_action(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        operation: str,
        idempotency_key: str,
        evidence_refs: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        verified = self._require_founder(principal)
        if not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise ValueError("a canonical idempotency key of 16 through 160 characters is required")
        if operation not in {"explain", "launch", "founder_complete", "capture_result"}:
            raise ValueError("unsupported founder action operation")
        if operation == "capture_result" and self.evidence_reviews is not None:
            raise PermissionError("pilot evidence must pass customer submission and operator review")
        scope = Scope(verified.tenant_id, verified.company_id or "")
        action = self._record(scope, action_id)
        if action.kind is not RecordKind.FOUNDER_ACTION or not action_id.startswith("founder_action_cleaning_"):
            raise PermissionError("action is outside the residential-cleaning pilot")
        inputs: dict[str, Any] = {
            "objective": f"Advance residential-cleaning founder action: {operation}",
            "operation": operation,
            "action_id": action_id,
            "actor_id": verified.user_id,
            "evidence_refs": evidence_refs or [],
        }
        job_key = f"cleaning-action:{idempotency_key}"
        existing = self.runtime_repository.get_job_by_idempotency(
            scope.tenant_id, scope.company_id, job_key
        )
        if existing is not None and existing.inputs != inputs:
            raise PilotConflict("idempotency key was already used for a different action command")
        budget_id = f"budget_action_{self._suffix(scope.company_id, idempotency_key)}"
        if self.runtime_repository.get_budget(scope.tenant_id, scope.company_id, budget_id) is None:
            self.runtime.budgets.create(
                Budget(budget_id, scope.tenant_id, scope.company_id, Money("USD", 0)),
                f"correlation_{self._suffix(idempotency_key)}",
            )
        job = self.runtime.create_job(
            tenant_id=scope.tenant_id,
            company_id=scope.company_id,
            capability=TRANSITION_CAPABILITY,
            inputs=inputs,
            budget_ref=budget_id,
            per_job_ceiling=Money("USD", 0),
            idempotency_key=job_key,
            correlation_id=f"correlation_{self._suffix(idempotency_key)}",
            approval_mode=(
                ApprovalMode.FOUNDER_ONLY
                if operation in {"founder_complete", "capture_result"}
                else ApprovalMode.AUTONOMOUS
            ),
            job_id=f"job_action_{self._suffix(scope.company_id, idempotency_key)}",
            provenance={"source": "authenticated_founder_action", "action_id": action_id},
        )
        if job.status is JobStatus.WAITING_APPROVAL:
            approval_id = job.approval_ids[0]
            approval = self.runtime_repository.get_approval(
                scope.tenant_id, scope.company_id, approval_id
            )
            if approval and approval.state is ApprovalState.REQUESTED:
                job = self.runtime.approve_job(
                    tenant_id=scope.tenant_id,
                    company_id=scope.company_id,
                    job_id=job.job_id,
                    approval_id=approval_id,
                    principal=verified,
                )
        if job.status is not JobStatus.SUCCEEDED:
            job = self.runtime.run(scope.tenant_id, scope.company_id, job.job_id)
        if job.status is not JobStatus.SUCCEEDED:
            raise PilotConflict("founder action transition was not completed")
        return self._public_founder_action(self._record(scope, action_id))

    def verify_founder_action_internal(
        self,
        *,
        tenant_id: str,
        company_id: str,
        action_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Internal Runtime/Verification path; intentionally not an HTTP route."""
        if self.evidence_verifier is None:
            raise PermissionError("no Business Builder evidence verifier is configured")
        if not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise ValueError("a canonical idempotency key is required")
        scope = Scope(tenant_id, company_id)
        self._record(scope, action_id)
        inputs = {
            "objective": "Verify one captured residential-cleaning founder action",
            "action_id": action_id,
        }
        job_key = f"cleaning-verification:{idempotency_key}"
        existing = self.runtime_repository.get_job_by_idempotency(
            tenant_id, company_id, job_key
        )
        if existing is not None and existing.inputs != inputs:
            raise PilotConflict("idempotency key was already used for different evidence")
        budget_id = f"budget_verify_{self._suffix(company_id, idempotency_key)}"
        if self.runtime_repository.get_budget(tenant_id, company_id, budget_id) is None:
            self.runtime.budgets.create(
                Budget(budget_id, tenant_id, company_id, Money("USD", 0)),
                f"correlation_{self._suffix(idempotency_key)}",
            )
        job = self.runtime.create_job(
            tenant_id=tenant_id,
            company_id=company_id,
            capability=VERIFY_CAPABILITY,
            inputs=inputs,
            budget_ref=budget_id,
            per_job_ceiling=Money("USD", 0),
            idempotency_key=job_key,
            correlation_id=f"correlation_{self._suffix(idempotency_key)}",
            approval_mode=ApprovalMode.AUTONOMOUS,
            job_id=f"job_verify_{self._suffix(company_id, idempotency_key)}",
            provenance={"source": "deterministic_test_verifier", "action_id": action_id},
        )
        if job.status is not JobStatus.SUCCEEDED:
            job = self.runtime.run(tenant_id, company_id, job.job_id)
        if job.status is not JobStatus.SUCCEEDED:
            raise PilotConflict("Verification did not accept the founder action")
        return self._public_founder_action(self._record(scope, action_id))

    def verify_founder_action_test_only(
        self,
        *,
        tenant_id: str,
        company_id: str,
        action_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Compatibility alias retained for the existing isolated proof suite."""
        return self.verify_founder_action_internal(
            tenant_id=tenant_id,
            company_id=company_id,
            action_id=action_id,
            idempotency_key=idempotency_key,
        )

    def project(self, principal: AuthenticatedPrincipal) -> dict[str, Any]:
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        scope = Scope(verified.tenant_id, verified.company_id or "")
        company = self.company_brain.get_company(scope)
        intake = self._record(scope, "intake_residential_cleaning_v1")
        research = self._record(scope, "research_residential_cleaning_denton_v1")
        proposed = self._record(scope, "recommendation_residential_cleaning_v1")
        workflow = self._record(scope, "pilot_residential_cleaning_v1")
        try:
            existing_audit = self.company_brain.repository.get_record(
                scope, "existing_business_audit_residential_cleaning_v1"
            )
        except NotFoundError:
            existing_audit = None
        try:
            existing_scope = self.company_brain.repository.get_record(
                scope, "existing_business_scope_residential_cleaning_v1"
            )
        except NotFoundError:
            existing_scope = None
        approval = self.runtime_repository.get_approval(
            scope.tenant_id, scope.company_id, workflow.data["approval_id"]
        )
        job = self.runtime_repository.get_job(
            scope.tenant_id, scope.company_id, workflow.data["job_id"]
        )
        founder_actions = [
            item
            for item in self.company_brain.query_current_state(
                scope, kinds=(RecordKind.FOUNDER_ACTION,)
            )
        ]
        order = None
        order_id = workflow.data.get("order_id")
        if order_id is None and intake.data.get("starting_point") == "running":
            candidates = [item for item in self.commercial_repository.list_current_orders(
                scope.tenant_id, scope.company_id
            ) if item.offer_code == OfferCode.EXISTING_RUN.value]
            if len(candidates) > 1:
                raise PilotConflict("multiple existing-business orders require operator reconciliation")
            order_id = candidates[0].order_id if candidates else None
        if isinstance(order_id, str):
            try:
                current = self.commercial_repository.get_order(
                    scope.tenant_id, scope.company_id, order_id
                )
            except CommercialNotFound:
                current = None
            if current is not None:
                order = {
                    "order_id": current.order_id,
                    "product_code": current.items[0].product_code.value,
                    "status": current.status.value,
                    "mode": workflow.data.get("commercial_mode"),
                    "quote_id": current.quote_id,
                    "quote_status": (
                        self.commercial_repository.get_quote(scope.tenant_id, scope.company_id, current.quote_id).status.value
                        if current.quote_id else None
                    ),
                    "payment_eligibility": current.eligibility.value,
                    "admission_present": current.admission_id is not None,
                }
        entitlements = [
            {
                "entitlement_id": item.grant_id,
                "code": item.entitlement_code,
                "class": item.entitlement_class.value,
                "status": item.status.value,
            }
            for item in self.commercial_repository.get_current_entitlement_grants(
                scope.tenant_id, scope.company_id
            )
            if item.source_order_id == order_id
        ] if order_id else []
        return {
            "pilot": "residential_cleaning_denton_v1",
            "company": {
                "company_id": company.scope.company_id,
                "display_name": company.display_name,
                "archetype": company.archetype,
                "jurisdiction": dict(company.jurisdiction),
                "lifecycle": company.lifecycle.value,
                "readiness": company.readiness,
            },
            "founder": {
                "user_id": verified.user_id,
                "organization_id": verified.organization_id,
                "membership_id": verified.membership_id,
                "role": verified.role.value,
            },
            "intake": self._public_record(intake),
            "research": self._public_record(research),
            "recommendation": self._public_record(proposed),
            "existing_business_audit": self._public_record(existing_audit) if existing_audit else None,
            "existing_business_scope": self._public_record(existing_scope) if existing_scope else None,
            "scope_commit": {
                "state": workflow.data["state"],
                "job_id": workflow.data["job_id"],
                "job_status": job.status.value if job else "missing",
                "approval_id": workflow.data["approval_id"],
                "approval_state": approval.state.value if approval else "missing",
            },
            "order": order,
            "entitlements": entitlements,
            "founder_actions": [self._public_founder_action_with_evidence(item) for item in founder_actions],
        }

    def _activate_test_order(self, context: AuthorizationContext, order):
        if order.status is OrderStatus.DRAFT:
            checkout = self.commercial.create_checkout(
                context,
                order.order_id,
                f"cleaning-pilot-checkout:{order.order_id}",
                provider_ref="opaque_test_checkout",
            )
            order = self.commercial_repository.get_order(
                context.tenant_id, context.company_id or "", order.order_id
            )
        else:
            checkout = self.commercial_repository.get_checkout_by_idempotency(
                context.tenant_id,
                context.company_id or "",
                f"cleaning-pilot-checkout:{order.order_id}",
            )
            if checkout is None:
                raise PilotConflict("paid pilot order is missing its checkout intent")
        if order.status is not OrderStatus.PAID:
            self.commercial.handle_billing_event(
                NormalizedBillingEvent(
                    event_id=f"billing_event_{self._suffix(order.order_id)}",
                    event_type="billing.payment.succeeded",
                    tenant_id=context.tenant_id,
                    user_id=context.actor_user_id,
                    company_id=context.company_id or "",
                    occurred_at=self.clock(),
                    provider="residential_cleaning_pilot_test",
                    provider_event_ref=f"test-payment:{order.order_id}",
                    correlation_id=f"correlation_{self._suffix(order.order_id, 'payment')}",
                    order_id=order.order_id,
                    checkout_intent_id=checkout.checkout_intent_id,
                    payment_provider_ref=f"opaque_test_payment:{order.order_id}",
                )
            )
        return self.commercial_repository.get_order(
            context.tenant_id, context.company_id or "", order.order_id
        )

    def _mark_scope_approval_complete(
        self, scope: Scope, user_id: str, approval_id: str
    ) -> None:
        action = self._record(scope, "founder_action_approve_cleaning_scope")
        if action.data.get("approval_id") != approval_id:
            raise PilotConflict("scope approval action does not match Runtime")
        evidence_ref = f"runtime-approval:{approval_id}"
        changed = {
            **dict(action.data),
            "state": "verified",
            "evidence_refs": [evidence_ref],
            "completed_by": user_id,
        }
        if dict(action.data) == changed:
            return
        self.company_brain.update_approved_state(
            scope,
            record_id=action.record_id,
            kind=action.kind,
            data=changed,
            knowledge_class=KnowledgeClass.FOUNDER_DECISION,
            provenance=action.provenance,
            confidence=None,
            owner_ref=action.owner_ref,
            expected_version=action.version,
        )

    def _validate_intake(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("intake must be an object")
        allowed = {
            "idea", "founder_display_name", "company_name", "organization_name",
            "country", "region", "locality", "service_radius_miles",
            "weekly_hours", "startup_budget_minor", "working_preferences",
            "starting_point",
        }
        if set(value) - allowed:
            raise ValueError("intake contains unsupported fields")
        result: dict[str, Any] = {}
        starting_point = value.get("starting_point", "idea")
        if starting_point not in {"idea", "started", "existing", "running"}:
            raise ValueError("starting_point is invalid")
        result["starting_point"] = starting_point
        for key, limit in (
            ("idea", 2000),
            ("founder_display_name", 160),
            ("company_name", 200),
        ):
            item = value.get(key)
            if not isinstance(item, str) or len(item.strip()) < (12 if key == "idea" else 1) or len(item) > limit:
                raise ValueError(f"{key} is invalid")
            result[key] = item.strip()
        organization_name = value.get("organization_name") or f"{result['company_name']} Organization"
        if not isinstance(organization_name, str) or not organization_name.strip() or len(organization_name) > 200:
            raise ValueError("organization_name is invalid")
        result["organization_name"] = organization_name.strip()
        for key, expected in SUPPORTED_JURISDICTION.items():
            supplied = value.get(key)
            if not isinstance(supplied, str) or supplied.strip().casefold() != expected.casefold():
                raise ValueError("this pilot supports only Denton, Texas, US")
            result[key] = expected
        radius = value.get("service_radius_miles")
        weekly = value.get("weekly_hours")
        budget = value.get("startup_budget_minor")
        if not isinstance(radius, int) or isinstance(radius, bool) or not 1 <= radius <= 25:
            raise ValueError("service_radius_miles must be between 1 and 25")
        if not isinstance(weekly, int) or isinstance(weekly, bool) or not 1 <= weekly <= 80:
            raise ValueError("weekly_hours must be between 1 and 80")
        if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= 10_000_000:
            raise ValueError("startup_budget_minor is invalid")
        preferences = value.get("working_preferences", {})
        if not isinstance(preferences, dict) or len(preferences) > 20:
            raise ValueError("working_preferences is invalid")
        encoded = json.dumps(preferences, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 4000:
            raise ValueError("working_preferences is too large")
        result.update(
            service_radius_miles=radius,
            weekly_hours=weekly,
            startup_budget_minor=budget,
            working_preferences=preferences,
        )
        return result

    def _ensure_record(
        self,
        scope: Scope,
        record_id: str,
        kind: RecordKind,
        data: dict[str, Any],
        knowledge_class: KnowledgeClass,
        provenance: tuple[Provenance, ...],
        owner: EntityRef,
        *,
        confidence: float | None = None,
    ):
        try:
            existing = self.company_brain.repository.get_record(scope, record_id)
        except NotFoundError:
            return self.company_brain.update_approved_state(
                scope,
                record_id=record_id,
                kind=kind,
                data=data,
                knowledge_class=knowledge_class,
                provenance=provenance,
                confidence=confidence,
                owner_ref=owner,
            )[0]
        if (
            existing.kind is not kind
            or dict(existing.data) != data
            or existing.knowledge_class is not knowledge_class
            or existing.owner_ref != owner
        ):
            raise PilotConflict(f"existing pilot record conflicts: {record_id}")
        return existing

    def _record(self, scope: Scope, record_id: str):
        try:
            return self.company_brain.repository.get_record(scope, record_id)
        except NotFoundError as exc:
            raise PilotNotFound("residential cleaning pilot not found") from exc

    @staticmethod
    def _public_record(record) -> dict[str, Any]:
        return {
            "record_id": record.record_id,
            "kind": record.kind.value,
            "knowledge_class": record.knowledge_class.value,
            "confidence": record.confidence,
            "data": dict(record.data),
            "version": record.version,
        }

    @staticmethod
    def _public_founder_action(record) -> dict[str, Any]:
        allowed = {
            "title", "reason", "instructions", "risk", "irreversible", "state",
            "selected", "critical", "responsibility", "partner_authority",
            "authority_target", "required_evidence_kinds", "evidence_refs", "blocks",
            "approval_id", "prepared_data", "destination", "evidence", "verification",
            "timestamps", "last_actor", "history", "action_key",
        }
        return {
            "founder_action_id": record.record_id,
            **{key: value for key, value in record.data.items() if key in allowed},
            "version": record.version,
        }

    def _public_founder_action_with_evidence(self, record) -> dict[str, Any]:
        result = self._public_founder_action(record)
        if self.evidence_reviews is not None and record.record_id in DEFINITIONS_BY_ID:
            result["evidence_review"] = self.evidence_reviews.public_action_state(
                record.scope.tenant_id, record.scope.company_id, record.record_id
            )
        return result

    @classmethod
    def _validate_responsibilities(cls, value: object) -> None:
        if isinstance(value, dict):
            responsibility = value.get("responsibility")
            if responsibility is not None and responsibility not in SUPPORTED_RESPONSIBILITIES:
                raise ValueError("unsupported responsibility classification")
            authority = value.get("authority")
            if authority is not None and authority not in SUPPORTED_RESPONSIBILITIES:
                raise ValueError("unsupported authority classification")
            for item in value.values():
                cls._validate_responsibilities(item)
        elif isinstance(value, list):
            for item in value:
                cls._validate_responsibilities(item)

    @staticmethod
    def _suffix(*values: str) -> str:
        return sha256(":".join(values).encode()).hexdigest()[:20]

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _require_founder(
        self, principal: AuthenticatedPrincipal
    ) -> AuthenticatedPrincipal:
        verified = self.principal_authority.verify(
            principal,
            tenant_id=principal.tenant_id,
            company_id=principal.company_id or "",
        )
        organization = self.identity_repository.get_organization(
            verified.organization_id
        )
        if verified.role is not Role.OWNER or organization.owner_user_id != verified.user_id:
            raise PermissionError("founder action requires the current founder/owner")
        return verified
