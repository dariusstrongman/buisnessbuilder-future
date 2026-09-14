from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    ResidentialCleaningJourneyService,
    ResidentialCleaningVerificationRouter,
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
            id_factory=self.ids,
            clock=lambda: self.now,
            enable_test_checkout=True,
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
        required = [item for item in actions.values() if item["state"] == "required"]
        self.assertGreaterEqual(len(required), 10)
        self.assertTrue(all(item["evidence_refs"] == [] for item in required))
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


if __name__ == "__main__":
    unittest.main()
