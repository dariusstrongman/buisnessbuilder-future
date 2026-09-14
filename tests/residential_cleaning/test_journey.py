from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http import HTTPStatus
from pathlib import Path
import tempfile
import unittest

from businessbuilder.commercial import (
    CommercialService,
    InMemoryCommercialRepository,
    ProductCode,
    RecordingCommercialEventSink,
    seed_default_catalog,
)
from businessbuilder.access_broker import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    ProviderReceipt,
    ReceiptStatus,
)
from businessbuilder.company_brain import (
    CompanyBrainService,
    KnowledgeClass,
    RecordKind,
    Scope,
    SQLiteCompanyBrainRepository,
)
from businessbuilder.company_brain.errors import NotFoundError
from businessbuilder.customer_api import CustomerApi
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    PrincipalContextAuthority,
    Role,
    SessionService,
    SQLiteIdentityRepository,
)
from businessbuilder.integration import (
    CompanyBrainRuntimeAdapter,
    CompanyBrainVerificationAdapter,
    IdentityApprovalPrincipalVerifier,
)
from businessbuilder.residential_cleaning import (
    PilotConflict,
    ResidentialCleaningJourneyService,
    ResidentialCleaningVerificationRouter,
)
from businessbuilder.residential_cleaning.founder_actions import (
    DeterministicResidentialCleaningEvidenceVerifier,
    EVIDENCE_CAPABILITY,
    FOUNDER_ACTION_DEFINITIONS,
)
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.ids import DeterministicIds
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import RecordingVerificationPort
from businessbuilder.runtime.storage import SQLiteRuntimeRepository
from businessbuilder.verification import (
    InMemoryVerificationRepository,
    ReadinessEvaluator,
    VerificationService,
    billy_bob_policy,
    default_registry,
)


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)


class ResidentialCleaningJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.ids = DeterministicIds()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_repository = SQLiteIdentityRepository(str(root / "identity.sqlite"))
        self.identity = IdentityService(
            self.identity_repository, id_factory=self.ids, clock=lambda: self.now
        )
        self.auth_provider = FakeDevAuthenticationProvider()
        self.sessions = SessionService(
            self.identity_repository,
            self.auth_provider,
            id_factory=self.ids,
            clock=lambda: self.now,
        )
        self.authority = PrincipalContextAuthority(
            self.identity_repository,
            clock=lambda: self.now,
            signing_key=b"cleaning-pilot-test-signing-key-32-bytes",
        )
        self.brain_repository = SQLiteCompanyBrainRepository(str(root / "brain.sqlite"))
        self.brain_repository.migrate()
        self.brain = CompanyBrainService(self.brain_repository)
        self.runtime_repository = SQLiteRuntimeRepository(str(root / "runtime.sqlite"))
        self.runtime_verification = RecordingVerificationPort()
        self.runtime = JobOrchestrator(
            repository=self.runtime_repository,
            registry=CapabilityRegistry(),
            company_reader=CompanyBrainRuntimeAdapter(self.brain),
            verification=ResidentialCleaningVerificationRouter(
                self.runtime_verification
            ),
            id_factory=self.ids,
            clock=lambda: self.now,
            approval_principals=IdentityApprovalPrincipalVerifier(self.authority),
        )
        self.verification_repository = InMemoryVerificationRepository()
        self.verification = VerificationService(
            self.verification_repository, default_registry()
        )
        self.snapshots = CompanyBrainVerificationAdapter(self.brain)
        self.commercial_repository = InMemoryCommercialRepository()
        seed_default_catalog(self.commercial_repository, effective_at=self.now)
        self.commercial = CommercialService(
            self.commercial_repository,
            self.identity.authorization,
            RecordingCommercialEventSink(),
            id_factory=self.ids,
            clock=lambda: self.now,
            auto_dispatch_outbox=False,
        )
        self.journey = ResidentialCleaningJourneyService(
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            company_brain=self.brain,
            runtime=self.runtime,
            runtime_repository=self.runtime_repository,
            commercial=self.commercial,
            commercial_repository=self.commercial_repository,
            verification=self.verification,
            id_factory=self.ids,
            clock=lambda: self.now,
            enable_test_checkout=True,
            evidence_verifier=DeterministicResidentialCleaningEvidenceVerifier(),
        )
        self.api = CustomerApi(
            identity_repository=self.identity_repository,
            principal_authority=self.authority,
            company_brain=self.brain,
            runtime=self.runtime,
            runtime_repository=self.runtime_repository,
            verification=self.verification,
            readiness=ReadinessEvaluator(
                self.verification_repository, billy_bob_policy()
            ),
            company_snapshots=self.snapshots,
            commercial=self.commercial,
            commercial_repository=self.commercial_repository,
            id_factory=self.ids,
            clock=lambda: self.now,
            residential_cleaning=self.journey,
        )
        self.founder, self.founder_token = self._user("founder@example.test")

    def tearDown(self) -> None:
        self.runtime_repository.close()
        self.brain_repository.close()
        self.identity_repository.close()
        self.temporary.cleanup()

    def _user(self, email: str):
        user = self.identity.register_user(email)
        proof = f"proof-{user.user_id}"
        self.auth_provider.register(user.user_id, email, proof)
        token = self.sessions.sign_in(email, proof, lifetime=timedelta(hours=1))[1]
        return user, token

    @staticmethod
    def _intake(company_name="Clear Day Cleaning"):
        return {
            "idea": "Start a trustworthy residential cleaning company for busy households.",
            "founder_display_name": "Pilot Founder",
            "organization_name": f"{company_name} Organization",
            "company_name": company_name,
            "country": "US",
            "region": "TX",
            "locality": "Denton",
            "service_radius_miles": 12,
            "weekly_hours": 30,
            "startup_budget_minor": 250000,
            "working_preferences": {
                "avoid_sundays": True,
                "owner_operated_at_launch": True,
            },
        }

    def request(self, method, path, *, token=None, body=None, headers=None):
        supplied = dict(headers or {})
        if token:
            supplied["Authorization"] = f"Bearer {token}"
        return self.api.handle(
            method=method,
            path=path,
            headers=supplied,
            query={},
            body=body,
            request_id="request_cleaning_pilot",
            correlation_id="correlation_cleaning_pilot",
        )

    def start(self, *, token=None, key="cleaning-pilot-request-0001", name=None):
        return self.request(
            "POST",
            "/api/v1/pilots/residential-cleaning/intakes",
            token=token or self.founder_token,
            body={"idempotency_key": key, "intake": self._intake(name or "Clear Day Cleaning")},
        )

    def approve_pilot(self):
        journey = self.start().body["journey"]
        company_id = journey["company"]["company_id"]
        approved = self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.OK, approved.status)
        membership = self.identity_repository.list_user_memberships(self.founder.user_id)[0]
        return membership.tenant_id, company_id, approved.body["journey"]

    def action_request(self, company_id, action_id, operation, key, *, evidence_refs=None, token=None):
        body = {"idempotency_key": key}
        if evidence_refs is not None:
            body["evidence_refs"] = evidence_refs
        return self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}/{operation}",
            token=token or self.founder_token,
            body=body,
        )

    def authority_evidence(self, tenant_id, company_id, action_key, reference):
        content_hash = sha256(reference.encode()).hexdigest()
        record = ArtifactRecord(
            artifact_id=reference,
            tenant_id=tenant_id,
            company_id=company_id,
            object_key=f"tenant/{tenant_id}/company/{company_id}/artifacts/{reference}/{content_hash}",
            content_sha256=content_hash,
            content_type="application/pdf",
            size_bytes=128,
            classification=ArtifactClassification.VERIFICATION_EVIDENCE,
            status=ArtifactStatus.AVAILABLE,
            provenance_ref=f"verified_authority:{action_key}",
            created_at=self.now,
        )
        self.runtime_repository.save_broker_record(
            "artifact", reference, tenant_id, company_id, record
        )
        return record

    def provider_evidence(self, tenant_id, company_id, operation, reference):
        receipt = ProviderReceipt(
            receipt_id=reference,
            tenant_id=tenant_id,
            company_id=company_id,
            job_id="job_pilot_provider_evidence",
            capability=EVIDENCE_CAPABILITY,
            provider="deterministic_sandbox_provider",
            operation=operation,
            provider_request_id=f"request_{reference}",
            idempotency_key=f"idempotency_{reference}",
            status=ReceiptStatus.SUCCEEDED,
            timestamp=self.now,
            response_classification="sandbox_authorized",
            external_object_ref=f"sandbox_{reference}",
            retryable=False,
            completed_at=self.now,
        )
        self.runtime_repository.claim_provider_receipt(receipt)
        return receipt

    def test_complete_founder_journey_uses_real_authorities(self) -> None:
        started = self.start()
        self.assertEqual(HTTPStatus.CREATED, started.status)
        journey = started.body["journey"]
        company_id = journey["company"]["company_id"]
        tenant_id = self.identity_repository.list_user_memberships(
            self.founder.user_id
        )[0].tenant_id

        membership = self.identity_repository.get_active_membership(
            tenant_id, self.founder.user_id
        )
        self.assertEqual(Role.OWNER, membership.role)
        self.assertEqual("residential_cleaning", journey["company"]["archetype"])
        self.assertEqual("challenged", journey["company"]["lifecycle"])
        self.assertFalse(journey["verification"]["ready"])
        self.assertFalse(journey["verification"]["fully_set"])
        self.assertEqual("verification", journey["verification"]["authority"])
        self.assertIsNone(journey["order"])
        self.assertEqual("requested", journey["scope_commit"]["approval_state"])

        research = journey["research"]["data"]
        self.assertEqual("bounded_prelaunch_research", research["status"])
        self.assertGreaterEqual(len(research["sources"]), 6)
        self.assertTrue(all(item["url"].startswith("https://") for item in research["sources"]))
        recommendation = journey["recommendation"]
        self.assertEqual(KnowledgeClass.INFERENCE.value, recommendation["knowledge_class"])
        for field in (
            "target_customer", "service_area", "offers", "starting_price_logic",
            "positioning", "risks", "startup_admin_requirements", "recommended_systems",
        ):
            self.assertIn(field, recommendation["data"])

        before = self.brain.compact_snapshot(Scope(tenant_id, company_id))
        self.assertFalse(
            any(
                item.get("record_id") == "decision_cleaning_build_scope"
                for item in before["records"]
            )
        )
        approved = self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.OK, approved.status)
        result = approved.body["journey"]
        self.assertEqual("assembly", result["company"]["lifecycle"])
        self.assertEqual("succeeded", result["scope_commit"]["job_status"])
        self.assertEqual(ProductCode.BUILD_BUSINESS.value, result["order"]["product_code"])
        self.assertEqual("fulfillment_pending", result["order"]["status"])
        self.assertEqual(
            {"website.build", "website.source_export", "company.build", "company.read_export", "artifacts.read_export"},
            {item["code"] for item in result["entitlements"]},
        )
        self.assertFalse(result["verification"]["ready"])
        self.assertFalse(result["verification"]["fully_set"])
        self.assertEqual([], self.runtime_verification.requests)

        actions = {item["founder_action_id"]: item for item in result["founder_actions"]}
        completed = actions["founder_action_approve_cleaning_scope"]
        self.assertEqual("verified", completed["state"])
        self.assertEqual(
            [f"runtime-approval:{journey['scope_commit']['approval_id']}"],
            completed["evidence_refs"],
        )
        prepared = [item for item in actions.values() if item["state"] == "prepared"]
        self.assertGreaterEqual(len(prepared), 11)
        self.assertTrue(all(item["evidence_refs"] == [] for item in prepared))
        self.assertTrue(all(item["prepared_data"]["checklist"] for item in prepared))
        self.assertTrue(all(item["destination"]["launch_mode"] == "prepared_handoff_only" for item in prepared))
        self.assertTrue(
            all(item["responsibility"] == "FOUNDER_ACTION" for item in actions.values())
        )

        build_room = self.request(
            "GET", f"/api/v1/companies/{company_id}/build-room", token=self.founder_token
        )
        self.assertEqual(HTTPStatus.OK, build_room.status)
        projection = build_room.body["build_room"]
        self.assertEqual(company_id, projection["company"]["company_id"])
        self.assertTrue(any(item["status"] == "succeeded" for item in projection["work_items"]))
        self.assertGreaterEqual(len(projection["founder_actions"]), len(actions))

        # Admission and approval are stable across retries: no second job, order,
        # checkout, or commercial event is created.
        repeated_start = self.start()
        self.assertEqual(company_id, repeated_start.body["journey"]["company"]["company_id"])
        repeated_approval = self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.OK, repeated_approval.status)
        self.assertEqual(1, len(self.runtime_repository.list_jobs(tenant_id, company_id)))
        self.assertEqual(1, len(self.commercial_repository.list_current_orders(tenant_id, company_id)))

    def test_customer_path_cannot_activate_payment_without_test_adapter(self) -> None:
        self.journey.enable_test_checkout = False
        started = self.start()
        journey = started.body["journey"]
        response = self.request(
            "POST",
            f"/api/v1/companies/{journey['company']['company_id']}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.OK, response.status)
        self.assertEqual("draft", response.body["journey"]["order"]["status"])
        self.assertEqual([], response.body["journey"]["entitlements"])
        self.assertEqual(
            "awaiting_authoritative_billing_event",
            response.body["journey"]["order"]["mode"],
        )

    def test_founder_approval_cannot_be_exercised_by_admin(self) -> None:
        started = self.start()
        journey = started.body["journey"]
        company_id = journey["company"]["company_id"]
        membership = self.identity_repository.list_user_memberships(self.founder.user_id)[0]
        admin, admin_token = self._user("admin@example.test")
        invited = self.identity.invite_member(
            AuthorizationContext(self.founder.user_id, membership.tenant_id, company_id),
            admin.user_id,
            Role.ADMIN,
        )
        self.identity.accept_membership(invited.membership_id, admin.user_id)

        response = self.request(
            "POST",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/approve",
            token=admin_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, response.status)
        self.assertTrue(
            any(
                item.action == "authorization.denied"
                and item.target_id == "founder_decision.approve"
                for item in self.identity_repository.list_audit(membership.tenant_id)
            )
        )
        status = self.request(
            "GET", f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
            token=self.founder_token,
        )
        self.assertEqual("requested", status.body["journey"]["scope_commit"]["approval_state"])

    def test_approval_is_bound_to_exact_recommendation_digest(self) -> None:
        started = self.start()
        journey = started.body["journey"]
        membership = self.identity_repository.list_user_memberships(self.founder.user_id)[0]
        scope = Scope(membership.tenant_id, journey["company"]["company_id"])
        record = self.brain_repository.get_record(
            scope, "recommendation_residential_cleaning_v1"
        )
        self.brain.update_approved_state(
            scope,
            record_id=record.record_id,
            kind=record.kind,
            data={**dict(record.data), "positioning": {"statement": "tampered"}},
            knowledge_class=record.knowledge_class,
            provenance=record.provenance,
            confidence=record.confidence,
            owner_ref=record.owner_ref,
            expected_version=record.version,
        )
        response = self.request(
            "POST",
            f"/api/v1/companies/{scope.company_id}/residential-cleaning-pilot/approve",
            token=self.founder_token,
            body={"approval_id": journey["scope_commit"]["approval_id"]},
        )
        self.assertEqual(HTTPStatus.CONFLICT, response.status)
        self.assertEqual(
            (),
            self.commercial_repository.list_current_orders(
                scope.tenant_id, scope.company_id
            ),
        )
        with self.assertRaises(NotFoundError):
            self.brain_repository.get_record(scope, "decision_cleaning_build_scope")

    def test_tenant_isolation_and_forged_authority_are_default_deny(self) -> None:
        first = self.start().body["journey"]
        company_id = first["company"]["company_id"]
        other, other_token = self._user("other-founder@example.test")
        second = self.start(
            token=other_token,
            key="cleaning-pilot-request-tenant-b",
            name="Tenant B Cleaning",
        )
        self.assertEqual(HTTPStatus.CREATED, second.status)
        self.assertNotEqual(company_id, second.body["journey"]["company"]["company_id"])

        for path in (
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
            f"/api/v1/companies/{company_id}/build-room",
        ):
            self.assertEqual(HTTPStatus.NOT_FOUND, self.request("GET", path, token=other_token).status)
        forged = self.request(
            "GET",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot",
            token=other_token,
            headers={"X-Tenant-Id": self.identity_repository.list_user_memberships(self.founder.user_id)[0].tenant_id},
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, forged.status)

    def test_input_is_narrow_and_cannot_smuggle_authority(self) -> None:
        self.assertEqual(
            HTTPStatus.UNAUTHORIZED,
            self.start(token="missing-session").status,
        )
        body = self._intake()
        body["tenant_id"] = "forged_tenant"
        response = self.request(
            "POST",
            "/api/v1/pilots/residential-cleaning/intakes",
            token=self.founder_token,
            body={"idempotency_key": "cleaning-pilot-forged-0001", "intake": body},
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, response.status)
        body = self._intake()
        body["locality"] = "Austin"
        response = self.request(
            "POST",
            "/api/v1/pilots/residential-cleaning/intakes",
            token=self.founder_token,
            body={"idempotency_key": "cleaning-pilot-wrong-city-01", "intake": body},
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, response.status)

    def test_entity_action_runs_prepared_through_independent_verification(self) -> None:
        tenant_id, company_id, journey = self.approve_pilot()
        action_id = "founder_action_cleaning_entity_admin"
        initial = next(
            item for item in journey["founder_actions"]
            if item["founder_action_id"] == action_id
        )
        self.assertEqual("prepared", initial["state"])
        self.assertIn("Texas Secretary of State", initial["destination"]["label"])
        self.assertEqual([], initial["evidence"])

        explained = self.action_request(
            company_id, action_id, "explain", "entity-action-explain-0001"
        )
        linked = self.action_request(
            company_id, action_id, "launch", "entity-action-launch-0001"
        )
        completed = self.action_request(
            company_id, action_id, "complete", "entity-action-complete-0001"
        )
        self.assertEqual("explained", explained.body["founder_action"]["state"])
        self.assertEqual("linked", linked.body["founder_action"]["state"])
        self.assertEqual("founder_completed", completed.body["founder_action"]["state"])
        self.assertEqual(
            {"founder_attestation"},
            {item["evidence_type"] for item in completed.body["founder_action"]["evidence"]},
        )
        self.assertNotEqual("verified", completed.body["founder_action"]["state"])

        evidence = self.authority_evidence(
            tenant_id,
            company_id,
            "texas_secretary_of_state",
            "artifact_entity_authority_result",
        )
        captured = self.action_request(
            company_id,
            action_id,
            "evidence",
            "entity-action-evidence-0001",
            evidence_refs=[{"kind": "artifact", "reference": evidence.artifact_id}],
        )
        self.assertEqual("result_captured", captured.body["founder_action"]["state"])
        self.assertEqual("not_requested", captured.body["founder_action"]["verification"]["state"])

        verified = self.journey.verify_founder_action_test_only(
            tenant_id=tenant_id,
            company_id=company_id,
            action_id=action_id,
            idempotency_key="entity-action-verification-0001",
        )
        self.assertEqual("verified", verified["state"])
        self.assertEqual("verification", verified["verification"]["authority"])
        self.assertEqual("accepted", verified["verification"]["result"])
        verification = self.verification.get(
            tenant_id, company_id, verified["verification"]["verification_id"]
        )
        self.assertEqual("verified", verification.state.value)
        self.assertEqual(
            {"founder_attestation", "authority_confirmation", "test_result"},
            {item.evidence_type.value for item in verification.evidence},
        )

        room = self.request(
            "GET", f"/api/v1/companies/{company_id}/build-room", token=self.founder_token
        )
        projected = next(
            item for item in room.body["build_room"]["founder_actions"]
            if item["founder_action_id"] == action_id
        )
        self.assertEqual("verified", projected["state"])
        self.assertEqual("accepted", projected["verification"]["result"])
        self.assertTrue(projected["prepared_data"]["checklist"])
        self.assertFalse(room.body["build_room"]["readiness"]["ready"])
        self.assertFalse(room.body["build_room"]["readiness"]["fully_set"])
        audit_actions = {
            item["action"]
            for item in self.runtime_repository.list_audit(tenant_id, company_id)
        }
        self.assertIn("job.created", audit_actions)
        self.assertIn("event.recorded", audit_actions)
        self.assertIn(
            "capability.completed",
            {
                item["type"]
                for item in self.runtime_repository.list_events(tenant_id, company_id)
            },
        )

    def test_every_pilot_action_has_concrete_destination_and_evidence_policy(self) -> None:
        _, _, journey = self.approve_pilot()
        actions = {
            item["action_key"]: item
            for item in journey["founder_actions"]
            if "action_key" in item
        }
        self.assertEqual(
            {item.key for item in FOUNDER_ACTION_DEFINITIONS}, set(actions)
        )
        expected = {
            "entity_admin": "authority_confirmation",
            "ein_tax_id": "authority_confirmation",
            "bank": "provider_receipt",
            "insurance": "provider_receipt",
            "licenses_permits": "authority_confirmation",
            "domain": "provider_receipt",
            "business_email": "provider_receipt",
            "crm": "provider_receipt",
            "scheduling": "provider_receipt",
            "payments": "provider_receipt",
            "legal_name_address": "founder_attestation",
        }
        for key, external_type in expected.items():
            self.assertEqual("prepared", actions[key]["state"])
            self.assertIn("founder_attestation", actions[key]["required_evidence_kinds"])
            self.assertIn("test_result", actions[key]["required_evidence_kinds"])
            self.assertIn(external_type, actions[key]["required_evidence_kinds"])
            self.assertTrue(actions[key]["prepared_data"]["checklist"])
            self.assertTrue(actions[key]["destination"]["label"])

    def test_authentic_scoped_provider_receipt_can_close_bank_action(self) -> None:
        tenant_id, company_id, _ = self.approve_pilot()
        action_id = "founder_action_cleaning_bank"
        self.action_request(company_id, action_id, "explain", "bank-explain-action-0001")
        self.action_request(company_id, action_id, "launch", "bank-launch-action-0001")
        self.action_request(company_id, action_id, "complete", "bank-complete-action-0001")
        receipt = self.provider_evidence(
            tenant_id, company_id, "bank.account_authorized", "receipt_bank_sandbox_result"
        )
        captured = self.action_request(
            company_id,
            action_id,
            "evidence",
            "bank-evidence-action-0001",
            evidence_refs=[{"kind": "provider_receipt", "reference": receipt.receipt_id}],
        )
        self.assertEqual("result_captured", captured.body["founder_action"]["state"])
        verified = self.journey.verify_founder_action_test_only(
            tenant_id=tenant_id,
            company_id=company_id,
            action_id=action_id,
            idempotency_key="bank-verification-action-0001",
        )
        self.assertEqual("verified", verified["state"])

    def test_founder_cannot_self_verify_or_skip_required_evidence(self) -> None:
        _, company_id, journey = self.approve_pilot()
        action_id = "founder_action_cleaning_entity_admin"
        self.assertEqual(
            HTTPStatus.NOT_FOUND,
            self.action_request(
                company_id, action_id, "verify", "founder-self-verify-0001"
            ).status,
        )
        skipped = self.action_request(
            company_id, action_id, "complete", "entity-action-skip-0001"
        )
        self.assertEqual(HTTPStatus.CONFLICT, skipped.status)
        self.action_request(company_id, action_id, "explain", "entity-explain-valid-0001")
        self.action_request(company_id, action_id, "launch", "entity-launch-valid-0001")
        self.action_request(company_id, action_id, "complete", "entity-complete-valid-0001")
        missing = self.action_request(
            company_id, action_id, "evidence", "entity-missing-evidence-0001",
            evidence_refs=[],
        )
        self.assertEqual(HTTPStatus.CONFLICT, missing.status)
        current = self.request(
            "GET",
            f"/api/v1/companies/{company_id}/residential-cleaning-pilot/founder-actions/{action_id}",
            token=self.founder_token,
        )
        self.assertEqual("founder_completed", current.body["founder_action"]["state"])

    def test_non_owner_cannot_advance_founder_action_and_denial_is_audited(self) -> None:
        tenant_id, company_id, _ = self.approve_pilot()
        admin, admin_token = self._user("action-admin@example.test")
        invited = self.identity.invite_member(
            AuthorizationContext(self.founder.user_id, tenant_id, company_id),
            admin.user_id,
            Role.ADMIN,
        )
        self.identity.accept_membership(invited.membership_id, admin.user_id)
        response = self.action_request(
            company_id,
            "founder_action_cleaning_entity_admin",
            "explain",
            "admin-action-explain-0001",
            token=admin_token,
        )
        self.assertEqual(HTTPStatus.FORBIDDEN, response.status)
        self.assertTrue(
            any(
                item.action == "authorization.denied"
                and item.actor_user_id == admin.user_id
                for item in self.identity_repository.list_audit(tenant_id)
            )
        )

    def test_forged_cross_scope_and_provider_evidence_fail_closed(self) -> None:
        _, company_id, _ = self.approve_pilot()
        action_id = "founder_action_cleaning_entity_admin"
        self.action_request(company_id, action_id, "explain", "entity-forged-explain-0001")
        self.action_request(company_id, action_id, "launch", "entity-forged-launch-0001")
        self.action_request(company_id, action_id, "complete", "entity-forged-complete-0001")

        other, other_token = self._user("evidence-other@example.test")
        second = self.start(
            token=other_token,
            key="evidence-other-pilot-0001",
            name="Other Evidence Cleaning",
        ).body["journey"]
        other_company = second["company"]["company_id"]
        other_tenant = self.identity_repository.list_user_memberships(other.user_id)[0].tenant_id
        foreign = self.authority_evidence(
            other_tenant,
            other_company,
            "texas_secretary_of_state",
            "artifact_foreign_authority_result",
        )
        forged_owner = self.action_request(
            company_id, action_id, "evidence", "entity-foreign-evidence-0001",
            evidence_refs=[{"kind": "artifact", "reference": foreign.artifact_id}],
        )
        self.assertEqual(HTTPStatus.CONFLICT, forged_owner.status)
        spoofed_provider = self.action_request(
            company_id, action_id, "evidence", "entity-spoofed-provider-0001",
            evidence_refs=[{"kind": "provider_receipt", "reference": "receipt_not_real"}],
        )
        self.assertEqual(HTTPStatus.CONFLICT, spoofed_provider.status)
        cross_tenant = self.action_request(
            company_id, action_id, "explain", "cross-tenant-action-0001",
            token=other_token,
        )
        self.assertEqual(HTTPStatus.NOT_FOUND, cross_tenant.status)

    def test_duplicate_capture_is_idempotent_and_verified_evidence_is_immutable(self) -> None:
        tenant_id, company_id, _ = self.approve_pilot()
        action_id = "founder_action_cleaning_entity_admin"
        self.action_request(company_id, action_id, "explain", "entity-idem-explain-0001")
        self.action_request(company_id, action_id, "launch", "entity-idem-launch-0001")
        self.action_request(company_id, action_id, "complete", "entity-idem-complete-0001")
        evidence = self.authority_evidence(
            tenant_id, company_id, "texas_secretary_of_state", "artifact_entity_idempotent"
        )
        refs = [{"kind": "artifact", "reference": evidence.artifact_id}]
        first = self.action_request(
            company_id, action_id, "evidence", "entity-idem-evidence-0001",
            evidence_refs=refs,
        )
        second = self.action_request(
            company_id, action_id, "evidence", "entity-idem-evidence-0001",
            evidence_refs=refs,
        )
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.body["founder_action"]["version"], second.body["founder_action"]["version"])
        verified = self.journey.verify_founder_action_test_only(
            tenant_id=tenant_id,
            company_id=company_id,
            action_id=action_id,
            idempotency_key="entity-idem-verification-0001",
        )
        verified_record = self.brain_repository.get_record(
            Scope(tenant_id, company_id), action_id
        )
        verified_digest = verified_record.data["verified_evidence_digest"]
        extra = self.authority_evidence(
            tenant_id, company_id, "texas_secretary_of_state", "artifact_entity_mutation"
        )
        mutation = self.action_request(
            company_id, action_id, "evidence", "entity-mutation-evidence-0001",
            evidence_refs=[*refs, {"kind": "artifact", "reference": extra.artifact_id}],
        )
        self.assertEqual(HTTPStatus.CONFLICT, mutation.status)
        current = self.brain_repository.get_record(Scope(tenant_id, company_id), action_id)
        self.assertEqual("verified", current.data["state"])
        self.assertEqual(verified_digest, current.data["verified_evidence_digest"])

        changed_evidence = [dict(item) for item in current.data["evidence"]]
        changed_evidence[0]["content_digest"] = "sha256:" + ("f" * 64)
        self.brain.update_approved_state(
            Scope(tenant_id, company_id),
            record_id=current.record_id,
            kind=current.kind,
            data={**dict(current.data), "evidence": changed_evidence},
            knowledge_class=current.knowledge_class,
            provenance=current.provenance,
            confidence=current.confidence,
            owner_ref=current.owner_ref,
            expected_version=current.version,
        )
        with self.assertRaises(PilotConflict):
            self.journey.verify_founder_action_test_only(
                tenant_id=tenant_id,
                company_id=company_id,
                action_id=action_id,
                idempotency_key="entity-mutated-verification-0001",
            )
        invalidated = self.brain_repository.get_record(
            Scope(tenant_id, company_id), action_id
        )
        self.assertEqual("result_captured", invalidated.data["state"])
        self.assertEqual("invalidated", invalidated.data["verification"]["state"])


if __name__ == "__main__":
    unittest.main()
