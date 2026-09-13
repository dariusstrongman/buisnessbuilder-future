from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from businessbuilder.ai_workforce import WorkforcePolicyService
from businessbuilder.commercial import CommercialService, seed_default_catalog
from businessbuilder.company_brain import CompanyBrainService
from businessbuilder.identity import AuthorizationPolicy, PrincipalContextAuthority
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
    PostgresWorkforceRepository,
)
from businessbuilder.runtime import CapabilityRegistry
from businessbuilder.runtime.ids import random_id
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.verification import VerificationService, default_registry

from .capabilities import DeterministicAgentCapability
from .model_router import ModelRouter
from .models import ModelCandidate
from .queue import QueuePort
from .service import AgentOutboxDispatcher, AgentRuntimeService, SharedAgentWorker
from businessbuilder.runtime import Money


SAFE_CAPABILITIES = (
    "customer.intake",
    "communications.email",
    "company.policy",
    "customer.quote",
    "customer.service_history",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PostgresAgentRuntime:
    identity_repository: PostgresIdentityRepository
    commercial_repository: PostgresCommercialRepository
    runtime_repository: PostgresRuntimeRepository
    verification_repository: PostgresVerificationRepository
    company_brain_repository: PostgresCompanyBrainRepository
    workforce_repository: PostgresWorkforceRepository
    company_brain: CompanyBrainService
    commercial: CommercialService
    workforce: WorkforcePolicyService
    runtime: JobOrchestrator
    service: AgentRuntimeService
    dispatcher: AgentOutboxDispatcher
    worker: SharedAgentWorker

    def close(self) -> None:
        for repository in (
            self.identity_repository,
            self.commercial_repository,
            self.runtime_repository,
            self.verification_repository,
            self.company_brain_repository,
            self.workforce_repository,
        ):
            repository.close()


def create_postgres_agent_runtime(
    *,
    queue: QueuePort,
    signing_key: bytes,
    worker_id: str,
    dispatcher_id: str,
    dsn: str | None = None,
    schema: str | None = None,
    clock=utc_now,
) -> PostgresAgentRuntime:
    if len(signing_key) < 32:
        raise ValueError("agent Runtime signing key must contain at least 32 bytes")
    identity_repository = PostgresIdentityRepository(dsn, schema=schema)
    commercial_repository = PostgresCommercialRepository(dsn, schema=schema)
    runtime_repository = PostgresRuntimeRepository(dsn, schema=schema)
    verification_repository = PostgresVerificationRepository(dsn, schema=schema)
    company_brain_repository = PostgresCompanyBrainRepository(dsn, schema=schema)
    workforce_repository = PostgresWorkforceRepository(dsn, schema=schema)
    company_brain = CompanyBrainService(company_brain_repository)
    snapshots = CompanyBrainVerificationAdapter(company_brain)
    verification = VerificationService(verification_repository, default_registry())
    authority = PrincipalContextAuthority(
        identity_repository, clock=clock, signing_key=signing_key
    )
    registry = CapabilityRegistry()
    for capability in SAFE_CAPABILITIES:
        registry.register(DeterministicAgentCapability(capability))
    runtime = JobOrchestrator(
        repository=runtime_repository,
        registry=registry,
        company_reader=CompanyBrainRuntimeAdapter(company_brain),
        verification=RuntimeVerificationAdapter(
            verification, snapshots, company_brain, definition_ids=(), clock=clock
        ),
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
    workforce = WorkforcePolicyService(
        workforce_repository, clock=clock, id_factory=random_id
    )

    def model_candidates(capability: str):
        return (
            ModelCandidate(
                "deterministic-test", "bounded-v1",
                frozenset({capability, "tools"}), 90, True, True, True, 25,
                Money("USD", 3 if capability == "communications.email" else 0),
            ),
        )

    service = AgentRuntimeService(
        runtime=runtime,
        repository=runtime_repository,
        identity_repository=identity_repository,
        principal_authority=authority,
        commercial_repository=commercial_repository,
        company_brain=company_brain,
        workforce=workforce,
        model_router=ModelRouter(),
        model_candidates=model_candidates,
        clock=clock,
        id_factory=random_id,
    )
    return PostgresAgentRuntime(
        identity_repository,
        commercial_repository,
        runtime_repository,
        verification_repository,
        company_brain_repository,
        workforce_repository,
        company_brain,
        commercial,
        workforce,
        runtime,
        service,
        AgentOutboxDispatcher(
            runtime_repository, queue, dispatcher_id=dispatcher_id, clock=clock
        ),
        SharedAgentWorker(
            service=service, queue=queue, worker_id=worker_id, clock=clock
        ),
    )
