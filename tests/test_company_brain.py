import tempfile
import unittest
from pathlib import Path

from businessbuilder.company_brain import (
    Company, CompanyBrainService, ConflictError, EntityRef, InvalidTransitionError,
    KnowledgeClass, LifecycleState, NotFoundError, Provenance, RecordKind, Scope,
    SQLiteCompanyBrainRepository, load_billy_bob,
)
from businessbuilder.company_brain.model import utc_now


class CompanyBrainTest(unittest.TestCase):
    def setUp(self):
        self.repository = SQLiteCompanyBrainRepository()
        self.repository.migrate()
        self.service = CompanyBrainService(self.repository)
        self.scope = load_billy_bob(self.service)
        self.founder = EntityRef("party", "party_billy")
        self.source = (Provenance("founder", utc_now(), self.founder, source_ref="test://intake"),)

    def test_billy_fixture_is_realistic_and_complete(self):
        company = self.service.get_company(self.scope)
        self.assertEqual(company.archetype, "mobile_service")
        records = self.service.query_current_state(self.scope)
        kinds = {record.kind for record in records}
        self.assertTrue({RecordKind.PARTY, RecordKind.OFFER, RecordKind.SERVICE, RecordKind.MARKET,
                         RecordKind.CAPACITY, RecordKind.ASSET, RecordKind.ACCOUNT,
                         RecordKind.FOUNDER_ACTION}.issubset(kinds))

    def test_cross_tenant_read_fails_closed(self):
        foreign = Scope("tenant_other", self.scope.company_id)
        with self.assertRaises(NotFoundError):
            self.service.get_company(foreign)
        with self.assertRaises(NotFoundError):
            self.repository.get_record(foreign, "offer_recurring_lawn")

    def test_cross_company_read_and_dependency_write_fail_closed(self):
        other_scope = Scope(self.scope.tenant_id, "co_other")
        self.service.create_company(Company(other_scope, "Other", "mobile_service", {"country":"US","region":"TX"}, (self.founder,), provenance=self.source))
        with self.assertRaises(NotFoundError):
            self.repository.get_record(other_scope, "offer_recurring_lawn")
        with self.assertRaises(NotFoundError):
            self.service.add_dependency(other_scope, source_record_id="offer_recurring_lawn", dependent_ref=EntityRef("verification","ver_x"), trigger="offer_changed")
        with self.assertRaises(ConflictError):
            self.service.record_fact(
                other_scope, record_id="offer_recurring_lawn", kind=RecordKind.OFFER,
                data={"name":"must not cross scope"}, provenance=self.source,
                owner_ref=self.founder, expected_version=1,
            )
        self.assertEqual(self.repository.get_record(self.scope, "offer_recurring_lawn").version, 1)

    def test_legal_lifecycle_transition_increments_version(self):
        scope = Scope("tenant_life", "co_life")
        company = Company(scope, "Life", "mobile_service", {"country":"US","region":"TX"}, (self.founder,), provenance=self.source)
        self.service.create_company(company)
        changed = self.service.transition_company(scope, LifecycleState.CHALLENGED, expected_version=1)
        self.assertEqual((changed.lifecycle, changed.version), (LifecycleState.CHALLENGED, 2))

    def test_illegal_lifecycle_transition_fails_loudly(self):
        scope = Scope("tenant_life2", "co_life2")
        self.service.create_company(Company(scope, "Life", "mobile_service", {"country":"US","region":"TX"}, (self.founder,), provenance=self.source))
        with self.assertRaisesRegex(InvalidTransitionError, "draft -> ready"):
            self.service.transition_company(scope, LifecycleState.READY, expected_version=1)

    def test_stale_company_write_rejected(self):
        scope = Scope("tenant_life3", "co_life3")
        self.service.create_company(Company(scope, "Life", "mobile_service", {"country":"US","region":"TX"}, (self.founder,), provenance=self.source))
        self.service.transition_company(scope, LifecycleState.CHALLENGED, expected_version=1)
        with self.assertRaises(ConflictError):
            self.service.transition_company(scope, LifecycleState.DRAFT, expected_version=1)

    def test_company_profile_history_is_append_only(self):
        self.service.add_dependency(
            self.scope, source_record_id="company.jurisdiction",
            dependent_ref=EntityRef("verification", "ver_jurisdiction", 1),
            trigger="jurisdiction_changed",
        )
        updated, notices = self.service.update_company_profile(
            self.scope, expected_version=1,
            jurisdiction={"country":"US", "region":"TX", "locality":"Denton"},
        )
        history = self.service.query_company_history(self.scope)
        self.assertEqual([item.version for item in history], [1, 2])
        self.assertEqual(history[0].jurisdiction["locality"], "Denton County")
        self.assertEqual(updated.jurisdiction["locality"], "Denton")
        self.assertEqual(notices[0].source_record_id, "company.jurisdiction")

    def test_owner_change_emits_invalidation_hook(self):
        self.service.add_dependency(
            self.scope, source_record_id="company.owners",
            dependent_ref=EntityRef("approval", "apr_owner_authority", 1),
            trigger="owner_changed",
        )
        new_owner = EntityRef("party", "party_billy_updated")
        _, notices = self.service.update_company_profile(
            self.scope, expected_version=1, owner_refs=(new_owner,),
        )
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].trigger, "owner_changed")

    def test_decision_history_is_preserved(self):
        first = self.repository.get_record(self.scope, "decision_launch_wedge")
        second = self.service.record_decision(self.scope, decision_id=first.record_id,
                                              data={"choice":"approved_with_8_mile_radius"}, provenance=self.source,
                                              owner_ref=self.founder, expected_version=first.version)
        history = self.service.query_history(self.scope, record_id=first.record_id)
        self.assertEqual([item.version for item in history], [1, 2])
        self.assertEqual(history[0].superseded_by_version, 2)
        self.assertEqual(second.supersedes_version, 1)

    def test_record_version_conflict_is_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.record_decision(self.scope, decision_id="decision_launch_wedge", data={"choice":"bad"},
                                         provenance=self.source, owner_ref=self.founder, expected_version=99)

    def test_provenance_survives_sqlite_round_trip(self):
        record = self.repository.get_record(self.scope, "offer_recurring_lawn")
        self.assertEqual(record.provenance[0].source_ref, "fixture://billy-bob-intake")
        self.assertEqual(record.provenance[0].actor_ref.id, "party_billy")

    def test_confidence_does_not_equal_verification(self):
        estimate = self.repository.get_record(self.scope, "capacity_weekly")
        self.assertEqual(estimate.knowledge_class, KnowledgeClass.ESTIMATE)
        self.assertEqual(estimate.confidence, .45)
        self.assertNotEqual(estimate.knowledge_class, KnowledgeClass.EXTERNAL_VERIFICATION)

    def test_inference_is_distinct_from_fact_and_verification(self):
        record = self.service.record_inference(
            self.scope, record_id="risk_route_density", kind=RecordKind.RISK,
            data={"hypothesis":"A tighter route may improve capacity"}, confidence=.6,
            provenance=self.source, owner_ref=self.founder,
        )
        self.assertEqual(record.knowledge_class, KnowledgeClass.INFERENCE)
        self.assertEqual(record.confidence, .6)

    def test_material_change_emits_dependent_invalidation(self):
        self.service.add_dependency(self.scope, source_record_id="market_denton_12mi",
                                    dependent_ref=EntityRef("verification", "ver_service_radius", 1),
                                    trigger="service_area_changed")
        prior = self.repository.get_record(self.scope, "market_denton_12mi")
        changed, notices = self.service.update_approved_state(
            self.scope, record_id=prior.record_id, kind=prior.kind,
            data={"center":"North Denton County, TX","radius_miles":8},
            knowledge_class=KnowledgeClass.FOUNDER_DECISION, provenance=self.source,
            confidence=None, owner_ref=self.founder, expected_version=1,
            invalidation_reason="Billy reduced the service radius",
        )
        self.assertEqual(changed.version, 2)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].dependent_ref.id, "ver_service_radius")
        self.assertEqual(self.repository.list_invalidations(self.scope), notices)

    def test_snapshot_is_deterministic_and_redacts_sensitive_keys(self):
        self.service.record_fact(self.scope, record_id="policy_sensitive", kind=RecordKind.POLICY,
                                 data={"public":"ok","api_token":"never expose"}, provenance=self.source,
                                 owner_ref=self.founder)
        one = self.service.compact_snapshot(self.scope)
        two = self.service.compact_snapshot(self.scope)
        self.assertEqual(one, two)
        row = next(item for item in one["records"] if item["id"] == "policy_sensitive")
        self.assertEqual(row["data"]["api_token"], "[REDACTED]")
        self.assertTrue(one["snapshot_digest"].startswith("sha256:"))

    def test_snapshot_excludes_accounts_founder_actions_and_audit(self):
        snapshot = self.service.compact_snapshot(self.scope)
        kinds = {item["kind"] for item in snapshot["records"]}
        self.assertNotIn("account", kinds)
        self.assertNotIn("founder_action", kinds)
        self.assertNotIn("audit_event", kinds)

    def test_file_database_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain.sqlite"
            repo = SQLiteCompanyBrainRepository(path)
            repo.migrate(); repo.migrate()
            count = repo.connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(count, 3)
            service = CompanyBrainService(repo)
            scope = load_billy_bob(service, tenant_id="tenant_persisted")
            repo.close()
            reopened = SQLiteCompanyBrainRepository(path)
            reopened.migrate()
            self.assertEqual(reopened.get_company(scope).display_name, "Billy Bob Lawn Care")
            self.assertEqual(reopened.get_record(scope, "offer_recurring_lawn").version, 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
