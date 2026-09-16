"""Seed an isolated PostgreSQL schema with a synthetic pilot company for review.

This exists so an operator surface can be inspected against the real backend
rather than a stub: it creates one tenant, one founder, one company with Company
Brain records, Runtime jobs and Verification records, and one SUPPORT operator
holding an owner-approved, read-only, expiring grant.

Nothing here is a production path. It writes only into the schema it is given.

    python scripts/seed_operator_preview_demo.py --schema operator_preview_demo
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.identity import (
    AuthorizationContext,
    FakeDevAuthenticationProvider,
    IdentityService,
    Permission,
    Role,
    SessionService,
)
from businessbuilder.postgres import (
    PostgresCompanyBrainRepository,
    PostgresIdentityRepository,
    PostgresRuntimeRepository,
    PostgresVerificationRepository,
)
from businessbuilder.integration import CompanyBrainRuntimeAdapter
from businessbuilder.runtime.capabilities import CapabilityRegistry
from businessbuilder.runtime.fakes import FakeCapability
from businessbuilder.runtime.ids import random_id
from businessbuilder.runtime.models import ApprovalMode, Budget, Money
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.ports import RecordingVerificationPort
from businessbuilder.verification import (
    VerificationMethod,
    VerificationRecord,
    VerificationService,
    VerificationState,
    default_registry,
)


COMPANY_ID = "company_operator_preview_demo"
SECOND_COMPANY_ID = "company_operator_preview_pilot_two"


def now() -> datetime:
    return datetime.now(timezone.utc)


def brain_company(brain, tenant_id, company_id, owner_id, display_name, lifecycle):
    stamp = now().isoformat().replace("+00:00", "Z")
    owner_ref = EntityRef("user", owner_id)
    return brain.create_company(
        Company(
            Scope(tenant_id, company_id),
            display_name,
            "residential_cleaning",
            {"country": "US", "region": "TX", "locality": "Denton"},
            (owner_ref,),
            lifecycle=lifecycle,
            provenance=(Provenance("seed", stamp, owner_ref, source_ref="seed://operator-preview"),),
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", default="operator_preview_demo")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.dsn:
        print("a --dsn or DATABASE_URL is required", file=sys.stderr)
        return 2

    identity_repository = PostgresIdentityRepository(args.dsn, schema=args.schema)
    identity = IdentityService(identity_repository, id_factory=random_id, clock=now)

    owner, _ = identity.register_founder("founder@preview.test", "Dana Reyes")
    tenant, organization, _ = identity.create_account(owner.user_id, "Clear Day Holdings")
    context = AuthorizationContext(owner.user_id, tenant.tenant_id)
    identity.attach_company(context, COMPANY_ID)
    identity.attach_company(context, SECOND_COMPANY_ID)

    operator = identity.register_user("operator@businessbuilder.test")
    invited = identity.invite_member(context, operator.user_id, Role.SUPPORT, reason="operator preview")
    identity.accept_membership(invited.membership_id, operator.user_id)
    grant = identity.grant_support_access(
        context,
        operator.user_id,
        frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS}),
        timedelta(hours=8),
        "Founder asked us to check why Ready is still false",
    )
    support_session = identity.start_support_session(
        operator.user_id, grant.grant_id, "operator dashboard preview review"
    )

    provider = FakeDevAuthenticationProvider()
    provider.register(operator.user_id, operator.email, "preview-proof")
    provider.register(owner.user_id, owner.email, "preview-proof")
    sessions = SessionService(identity_repository, provider, id_factory=random_id, clock=now)
    _, operator_token = sessions.sign_in(operator.email, "preview-proof", lifetime=timedelta(hours=8))
    _, founder_token = sessions.sign_in(owner.email, "preview-proof", lifetime=timedelta(hours=8))
    identity_repository.close()

    brain_repository = PostgresCompanyBrainRepository(args.dsn, schema=args.schema)
    brain = CompanyBrainService(brain_repository)
    brain_company(brain, tenant.tenant_id, COMPANY_ID, owner.user_id, "Clear Day Cleaning", LifecycleState.ASSEMBLY)
    brain_company(brain, tenant.tenant_id, SECOND_COMPANY_ID, owner.user_id, "Northgate Home Care", LifecycleState.DRAFT)

    scope = Scope(tenant.tenant_id, COMPANY_ID)
    stamp = now().isoformat().replace("+00:00", "Z")
    owner_ref = EntityRef("user", owner.user_id)
    seeded = [
        (RecordKind.GOAL, "goal_launch", {"statement": "Serve 20 recurring Denton households"}),
        (RecordKind.MARKET, "market_denton", {"area": "Denton, TX", "radius_miles": 12}),
        (RecordKind.OFFER, "offer_recurring", {"name": "Recurring clean", "cadence": "fortnightly"}),
        (RecordKind.OFFER, "offer_deep", {"name": "Deep clean", "cadence": "one_off"}),
        (RecordKind.SERVICE, "service_standard", {"name": "Standard clean"}),
        (RecordKind.POLICY, "policy_hours", {"avoid_sundays": True}),
        (RecordKind.RISK, "risk_capacity", {"statement": "Owner-operated capacity caps weekly jobs"}),
        (RecordKind.OBLIGATION, "obligation_insurance", {"statement": "General liability before first job"}),
    ]
    for kind, record_id, data in seeded:
        brain.record_fact(
            scope,
            record_id=record_id,
            kind=kind,
            data=data,
            provenance=(Provenance("seed", stamp, owner_ref, source_ref="seed://operator-preview"),),
            owner_ref=owner_ref,
        )
    runtime_repository = PostgresRuntimeRepository(args.dsn, schema=args.schema)
    runtime_repository.save_budget(
        Budget("budget_operator_preview", tenant.tenant_id, COMPANY_ID, Money("USD", 25_000))
    )
    registry = CapabilityRegistry()
    for capability in ("website.assemble", "identity.brand_direction", "customer.lead_intake"):
        registry.register(FakeCapability(capability, estimate_minor=900))
    runtime = JobOrchestrator(
        repository=runtime_repository,
        registry=registry,
        company_reader=CompanyBrainRuntimeAdapter(brain),
        verification=RecordingVerificationPort(),
        id_factory=random_id,
        clock=now,
    )
    jobs = [
        ("website.assemble", "Assemble the service and quote pages", ApprovalMode.AUTONOMOUS),
        ("identity.brand_direction", "Prepare the brand direction for founder approval", ApprovalMode.FOUNDER_ONLY),
        ("customer.lead_intake", "Connect the quote form to the lead record", ApprovalMode.AUTONOMOUS),
    ]
    for index, (capability, objective, mode) in enumerate(jobs, 1):
        runtime.create_job(
            tenant_id=tenant.tenant_id,
            company_id=COMPANY_ID,
            capability=capability,
            inputs={"objective": objective},
            budget_ref="budget_operator_preview",
            per_job_ceiling=Money("USD", 900),
            idempotency_key=f"operator-preview-seed-job-{index:04d}",
            correlation_id="operator-preview-seed",
            approval_mode=mode,
        )
    runtime_repository.close()

    # Verification records are created in their opening state only. Ready and
    # Fully Set must stay false until real evidence exists, and a seed is not
    # evidence, so nothing here is transitioned to VERIFIED.
    verification_repository = PostgresVerificationRepository(args.dsn, schema=args.schema)
    verification = VerificationService(verification_repository, default_registry())
    for definition_id, scope_text in (
        ("website.deployed", "Production deployment answers independently"),
        ("website.https", "Production TLS is valid for the owned hostname"),
        ("crm.lead_capture", "A lead is stored, routed and retrievable"),
    ):
        verification.create(
            VerificationRecord(
                random_id("verification"),
                tenant.tenant_id,
                COMPANY_ID,
                definition_id,
                "company",
                COMPANY_ID,
                VerificationState.PROPOSED,
                "Verification",
                scope_text,
                VerificationMethod.AUTOMATED,
            )
        )
    verification_repository.close()

    # One prepared Founder Action, recorded exactly as the product records them.
    founder_actions = [
        {
            "action_key": "entity_admin",
            "title": "Complete the entity and administrative path",
            "reason": "Only the founder can attest and file with the relevant authority.",
            "instructions": ["Review the prepared checklist", "Use the official authority destination"],
            "state": "prepared",
            "responsibility": "FOUNDER_ACTION",
            "partner_authority": "EXTERNAL_PROVIDER/AUTHORITY",
            "required_evidence_kinds": ["founder_attestation", "authority_confirmation"],
        },
        {
            "action_key": "business_banking",
            "title": "Open the business bank account",
            "reason": "A bank must verify the founder's identity directly; nobody can do it for them.",
            "instructions": ["Bring the formation document and tax ID"],
            "state": "explained",
            "responsibility": "FOUNDER_ACTION",
            "partner_authority": "EXTERNAL_PROVIDER",
            "required_evidence_kinds": ["provider_receipt"],
        },
    ]
    for action in founder_actions:
        brain.append_founder_action(
            scope,
            action_id=f"founder_action_{action['action_key']}",
            data=action,
            provenance=(Provenance("seed", stamp, owner_ref, source_ref="seed://operator-preview"),),
            owner_ref=owner_ref,
        )
    brain_repository.close()

    print(json.dumps({
        "schema": args.schema,
        "tenant_id": tenant.tenant_id,
        "organization_id": organization.organization_id,
        "company_id": COMPANY_ID,
        "second_company_id": SECOND_COMPANY_ID,
        "operator_user_id": operator.user_id,
        "operator_token": operator_token,
        "founder_token": founder_token,
        "grant_id": grant.grant_id,
        "support_session_id": support_session.impersonation_session_id,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
