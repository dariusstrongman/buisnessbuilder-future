from __future__ import annotations

import unittest

from verification.catalog import default_registry
from verification.events import CanonicalEvent, VerificationEventHandler
from verification.fixtures import COMPANY_ID, FIXTURE_NOW, TENANT_ID, evidence, verify_definition
from verification.models import DependencyRef, EvidenceType, ExternalRecord, ExternalState, VerificationState
from verification.repository import InMemoryVerificationRepository
from verification.service import VerificationService


class ExternalAndEventTests(unittest.TestCase):
    def test_founder_attestation_is_not_external_authority_verification(self) -> None:
        attested = ExternalRecord(
            external_record_id="external_llc",
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            subject="llc formation",
            state=ExternalState.FOUNDER_ATTESTED,
            evidence=(evidence("evidence_llc_attestation", EvidenceType.FOUNDER_ATTESTATION),),
            authority=None,
        )
        self.assertFalse(attested.is_authoritatively_verified(FIXTURE_NOW))

    def test_authority_confirmation_supports_external_verification(self) -> None:
        observed = ExternalRecord(
            external_record_id="external_license",
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            subject="license",
            state=ExternalState.EXTERNALLY_VERIFIED,
            evidence=(evidence("evidence_license_authority", EvidenceType.AUTHORITY_CONFIRMATION, issuer="fictional-issuing-authority"),),
            authority="fictional-issuing-authority",
        )
        self.assertTrue(observed.is_authoritatively_verified(FIXTURE_NOW))

    def test_mismatched_issuer_cannot_supply_external_authority(self) -> None:
        observed = ExternalRecord(
            external_record_id="external_bank",
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            subject="banking",
            state=ExternalState.EXTERNALLY_VERIFIED,
            evidence=(evidence("evidence_bank_other", EvidenceType.PROVIDER_RECEIPT, issuer="other-provider"),),
            authority="selected-bank",
        )
        self.assertFalse(observed.is_authoritatively_verified(FIXTURE_NOW))

    def test_event_handler_invalidates_once(self) -> None:
        repo = InMemoryVerificationRepository()
        registry = default_registry()
        service = VerificationService(repo, registry)
        record = verify_definition(
            service,
            registry,
            "website.links",
            77,
            dependencies=(DependencyRef("website_deployment", 1, "website_deployment"),),
        )
        handler = VerificationEventHandler(service)
        event = CanonicalEvent(
            event_id="event_deploy_002",
            tenant_id=TENANT_ID,
            company_id=COMPANY_ID,
            event_type="website.deployment.changed",
            occurred_at=FIXTURE_NOW,
            payload={"dependency_id": "website_deployment", "new_version": 2},
        )
        first = handler.handle(event)
        second = handler.handle(event)
        self.assertEqual(first[0].state, VerificationState.EXECUTED)
        self.assertEqual(second, ())
        self.assertGreater(first[0].version, record.version)

    def test_service_area_change_invalidates_geography_bound_verification(self) -> None:
        repo = InMemoryVerificationRepository()
        registry = default_registry()
        service = VerificationService(repo, registry)
        verify_definition(
            service,
            registry,
            "scheduling.booking",
            78,
            dependencies=(DependencyRef("service_area", 3, "market"),),
        )
        handler = VerificationEventHandler(service)
        changed = handler.handle(
            CanonicalEvent(
                event_id="event_service_area_004",
                tenant_id=TENANT_ID,
                company_id=COMPANY_ID,
                event_type="service_area.changed",
                occurred_at=FIXTURE_NOW,
                payload={"dependency_id": "service_area", "new_version": 4},
            )
        )
        self.assertEqual(changed[0].state, VerificationState.EXECUTED)


if __name__ == "__main__":
    unittest.main()
