from __future__ import annotations

from datetime import datetime, timezone

from businessbuilder.commercial import CommercialService, seed_default_catalog
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
from businessbuilder.identity import AuthorizationPolicy, PrincipalContextAuthority

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
        verification=verification_port,
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
    )
    seed_default_catalog(commercial_repository, effective_at=clock())
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
    )
