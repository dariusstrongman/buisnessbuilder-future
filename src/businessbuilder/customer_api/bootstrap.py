from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from businessbuilder.commercial import CommercialService, seed_default_catalog
from businessbuilder.commercial.stripe_webhooks import StripeWebhookIngress
from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority
from businessbuilder.commercial.paid_pilot_release import PaidPilotReleaseGate
from businessbuilder.company_brain import CompanyBrainService
from businessbuilder.integration import (
    CompanyBrainRuntimeAdapter,
    CompanyBrainVerificationAdapter,
    IdentityApprovalPrincipalVerifier,
    RuntimeVerificationAdapter,
)
from businessbuilder.integration.commercial import RuntimeCommercialEventSink
from businessbuilder.postgres import (
    PostgresCommercialRepository,
    PostgresCompanyBrainRepository,
    PostgresIdentityRepository,
    PostgresRuntimeRepository,
    PostgresVerificationRepository,
)
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.ids import random_id
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.verification import (
    ReadinessEvaluator,
    VerificationService,
    billy_bob_policy,
    default_registry,
)
from businessbuilder.identity import (
    AuthorizationPolicy,
    PrincipalContextAuthority,
    ProductionFounderSessionService,
)
from businessbuilder.residential_cleaning import (
    ResidentialCleaningJourneyService,
    ResidentialCleaningVerificationRouter,
)

from .application import CustomerApi


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_postgres_customer_api(
    *,
    signing_key: bytes,
    dsn: str | None = None,
    schema: str | None = None,
    provider_connections=None,
    outbound_communications=None,
    communications_compliance=None,
    live_canary_readiness=None,
    enable_residential_cleaning_test_checkout: bool = False,
    allow_supervised_stripe_test_admission: bool = False,
    residential_cleaning_evidence_verifier=None,
    residential_cleaning_evidence_store=None,
    residential_cleaning_malware_scanner=None,
    founder_authentication_provider=None,
    payment_provider=None,
    checkout_success_url: str | None = None,
    checkout_cancel_url: str | None = None,
    commercial_operator_provisioner_verifier=None,
    commercial_tax_authority_verifier=None,
    paid_pilot_release_authority_verifier=None,
    paid_pilot_packet_evidence_verifier=None,
    clock=utc_now,
) -> CustomerApi:
    """Production-shaped composition root; authentication provider remains external."""
    if len(signing_key) < 32:
        raise ValueError("customer API principal signing key must be at least 32 bytes")
    identity_repository = PostgresIdentityRepository(dsn, schema=schema)
    commercial_repository = PostgresCommercialRepository(dsn, schema=schema)
    runtime_repository = PostgresRuntimeRepository(dsn, schema=schema)
    verification_repository = PostgresVerificationRepository(dsn, schema=schema)
    brain_repository = PostgresCompanyBrainRepository(dsn, schema=schema)

    company_brain = CompanyBrainService(brain_repository)
    snapshots = CompanyBrainVerificationAdapter(company_brain)
    verification = VerificationService(verification_repository, default_registry())
    verification_port = RuntimeVerificationAdapter(
        verification, snapshots, company_brain, clock=clock
    )
    authority = PrincipalContextAuthority(
        identity_repository, clock=clock, signing_key=signing_key
    )
    runtime = JobOrchestrator(
        repository=runtime_repository,
        registry=CapabilityRegistry(),
        company_reader=CompanyBrainRuntimeAdapter(company_brain),
        verification=ResidentialCleaningVerificationRouter(verification_port),
        id_factory=random_id,
        clock=clock,
        approval_principals=IdentityApprovalPrincipalVerifier(authority),
    )
    commercial = CommercialService(
        commercial_repository,
        AuthorizationPolicy(identity_repository),
        RuntimeCommercialEventSink(runtime.events),
        id_factory=random_id,
        clock=clock,
        allow_test_admission=(enable_residential_cleaning_test_checkout
                              or allow_supervised_stripe_test_admission),
        operator_authority=CommercialOperatorAuthority(
            identity_repository, commercial_repository,
            signing_key=sha256(b"commercial-operator-v1:" + signing_key).digest(),
            clock=clock,
            privileged_provisioner_verifier=commercial_operator_provisioner_verifier,
        ),
        tax_authority_verifier=commercial_tax_authority_verifier,
        paid_pilot_release_gate=PaidPilotReleaseGate(
            commercial_repository, clock=clock,
            authority_verifier=paid_pilot_release_authority_verifier,
            packet_evidence_verifier=paid_pilot_packet_evidence_verifier,
        ),
    )
    seed_default_catalog(commercial_repository, effective_at=clock())
    residential_cleaning = ResidentialCleaningJourneyService(
        identity_repository=identity_repository,
        principal_authority=authority,
        company_brain=company_brain,
        runtime=runtime,
        runtime_repository=runtime_repository,
        commercial=commercial,
        commercial_repository=commercial_repository,
        verification=verification,
        id_factory=random_id,
        clock=clock,
        enable_test_checkout=enable_residential_cleaning_test_checkout,
        evidence_verifier=residential_cleaning_evidence_verifier,
        evidence_store=residential_cleaning_evidence_store,
        malware_scanner=residential_cleaning_malware_scanner,
    )
    founder_authentication = (
        ProductionFounderSessionService(
            identity_repository,
            founder_authentication_provider,
            id_factory=random_id,
            clock=clock,
        )
        if founder_authentication_provider is not None
        else None
    )
    return CustomerApi(
        identity_repository=identity_repository,
        principal_authority=authority,
        company_brain=company_brain,
        runtime=runtime,
        runtime_repository=runtime_repository,
        verification=verification,
        readiness=ReadinessEvaluator(verification_repository, billy_bob_policy()),
        company_snapshots=snapshots,
        commercial=commercial,
        commercial_repository=commercial_repository,
        id_factory=random_id,
        clock=clock,
        provider_connections=provider_connections,
        outbound_communications=outbound_communications,
        communications_compliance=communications_compliance,
        live_canary_readiness=live_canary_readiness,
        residential_cleaning=residential_cleaning,
        founder_authentication=founder_authentication,
        payment_provider=payment_provider,
        payment_webhooks=(StripeWebhookIngress(commercial_repository, commercial, payment_provider)
                          if payment_provider is not None else None),
        checkout_success_url=checkout_success_url,
        checkout_cancel_url=checkout_cancel_url,
        commercial_operator_authority=commercial.operator_authority,
    )
