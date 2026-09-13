from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from businessbuilder.setup_admin import (
    ActorBinding,
    ApprovalBinding,
    AuthorityGate,
    AuthorityError,
    Criticality,
    EvidenceReference,
    IllegalTransitionError,
    MissingEvidenceError,
    NotFoundError,
    Provenance,
    RequirementCatalog,
    RequirementDefinition,
    SetupAdminError,
    SetupAdminService,
    SetupMode,
    SetupState,
    billy_bob_setup,
    approval_subject_digest,
)
from businessbuilder.setup_admin.repository import InMemorySetupAdminRepository
from businessbuilder.runtime.contracts import ContractValidator


NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
TENANT = "tenant_billy"
COMPANY = "co_billy_bob_lawn"
CORRELATION = "correlation_billy_setup"


def item_for(service, requirement_id: str):
    return next(item for item in service.repository.list_items(TENANT, COMPANY) if item.requirement_id == requirement_id)


def actor(role="founder", actor_id="party_billy", *, verified=True, tenant=TENANT, company=COMPANY):
    return ActorBinding(tenant, company, role, actor_id, verified, f"identity_{actor_id}")


def approval_for(item, mode, *, target=None, amount_minor_units=None, currency=None, who=None):
    who = who or actor()
    target = target or f"target_{item.requirement_id.replace('.', '_')}"
    if item.requirement_id == "admin.domain_account":
        amount_minor_units = 1200 if amount_minor_units is None else amount_minor_units
        currency = currency or "USD"
    return ApprovalBinding(
        TENANT,
        COMPANY,
        f"approval_{item.requirement_id.replace('.', '_')}",
        approval_subject_digest(item, mode, target, amount_minor_units, currency),
        who.actor_id,
        who.actor_type,
        "granted",
        target,
        NOW + timedelta(hours=1),
        amount_minor_units,
        currency,
    )


def evidence(item, kind, *, source="founder", suffix="1", verified=True, tenant=TENANT, company=COMPANY):
    source_id = "party_billy" if source == "founder" else ("authority_fictional" if source == "external_authority" else "provider_fictional")
    if source == "system":
        source_id = "setup_admin"
    source_actor = ActorBinding(tenant, company, source, source_id, verified, f"identity_{source_id}")
    bound = {}
    if item.current_founder_action_id:
        approval = approval_for(item, item.mode)
        bound = {
            "founder_action_id": item.current_founder_action_id,
            "approval_ref": approval.approval_ref,
            "approval_subject_digest": approval.subject_digest,
            "exact_target": approval.target,
            "amount_minor_units": approval.amount_minor_units,
            "currency": approval.currency,
        }
    return EvidenceReference(
        tenant, company, f"evidence_{kind}_{suffix}", kind, source,
        item.setup_item_id, verified, NOW, source_id, f"verification_{source_id}",
        authorized_actor=source_actor, **bound,
    )


def system_actor(actor_id="setup_admin", *, verified=True):
    return actor("system", actor_id, verified=verified)


def choose(service, item, mode=SetupMode.DO_IT, suffix="1"):
    who = actor()
    return service.choose_mode(
        TENANT,
        COMPANY,
        item.setup_item_id,
        mode,
        actor=who,
        approval=None if mode is SetupMode.SKIP else approval_for(item, mode, who=who),
        idempotency_key=f"choose-mode-{item.requirement_id}-{suffix}",
        correlation_id=CORRELATION,
        at=NOW,
    )


def test_billy_bob_catalog_is_mobile_locality_scoped_and_advisory_safe():
    service, items = billy_bob_setup()
    requirement_ids = {item.requirement_id for item in items}
    assert requirement_ids == {
        "admin.domain_account",
        "admin.business_formation",
        "admin.insurance_review",
        "admin.service_scope_attestation",
        "admin.account_recovery",
    }
    assert all(item.tenant_id == TENANT and item.company_id == COMPANY for item in items)
    assert all("advice" not in service.catalog.get(item.requirement_id).description.lower() for item in items)


def test_do_it_creates_founder_action_without_claiming_execution():
    service, _ = billy_bob_setup()
    formation = item_for(service, "admin.business_formation")
    selected = choose(service, formation)
    assert selected.state is SetupState.PLANNED
    action = next(action for action in service.repository.list_actions(TENANT, COMPANY) if action.setup_item_id == formation.setup_item_id)
    assert action.action_type == "file"
    assert action.state == "required"
    assert action.irreversible is True
    assert "yourself" in " ".join(action.instructions)


def test_guide_me_uses_the_same_founder_authority_gate():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.insurance_review"), SetupMode.GUIDE_ME)
    waiting = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor("guide_agent"), idempotency_key="begin-guide", correlation_id=CORRELATION, at=NOW)
    assert waiting.state is SetupState.WAITING_FOUNDER


def test_skip_is_visible_and_blocks_fully_set():
    service, _ = billy_bob_setup()
    formation = item_for(service, "admin.business_formation")
    skipped = choose(service, formation, SetupMode.SKIP)
    projection = service.projection(TENANT, COMPANY)
    assert skipped.state is SetupState.SKIPPED
    assert projection.fully_set_eligible is False
    assert "admin.business_formation: skipped" in projection.blockers
    assert any("Formation remains explicitly unresolved" in text for text in projection.consequences)


def test_cross_tenant_and_cross_company_reads_fail_closed():
    service, _ = billy_bob_setup()
    item = item_for(service, "admin.domain_account")
    with pytest.raises(NotFoundError):
        service.repository.get_item("tenant_other", COMPANY, item.setup_item_id)
    with pytest.raises(NotFoundError):
        service.repository.get_item(TENANT, "co_other", item.setup_item_id)
    assert service.repository.list_items("tenant_other", COMPANY) == ()


def test_illegal_transitions_are_rejected():
    service, _ = billy_bob_setup()
    item = item_for(service, "admin.domain_account")
    with pytest.raises(IllegalTransitionError):
        service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor("x"), idempotency_key="illegal-begin", correlation_id=CORRELATION, at=NOW)
    choose(service, item)
    with pytest.raises(IllegalTransitionError):
        choose(service, item, SetupMode.SKIP, suffix="2")


@pytest.mark.parametrize(
    "requirement_id,forbidden_action",
    [
        ("admin.business_formation", "file"),
        ("admin.domain_account", "purchase"),
        ("admin.insurance_review", "choose_insurance"),
        ("admin.service_scope_attestation", "attest_license"),
    ],
)
def test_ai_cannot_file_purchase_choose_or_attest(requirement_id: str, forbidden_action: str):
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, requirement_id))
    service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key=f"begin-{requirement_id}", correlation_id=CORRELATION, at=NOW)
    action = next(action for action in service.repository.list_actions(TENANT, COMPANY) if action.setup_item_id == item.setup_item_id)
    assert action.action_type == forbidden_action
    with pytest.raises(AuthorityError):
        service.complete_as_ai(TENANT, COMPANY, item.setup_item_id, evidence=(evidence(item, "unsafe"),), idempotency_key=f"ai-{requirement_id}", correlation_id=CORRELATION, at=NOW)
    with pytest.raises(AuthorityError):
        service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor("agent", "setup_agent"), evidence=(evidence(item, "founder_attestation"),), idempotency_key=f"submit-{requirement_id}", correlation_id=CORRELATION, at=NOW)


def test_external_gate_requires_founder_then_authority_evidence():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.business_formation"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="formation-begin", correlation_id=CORRELATION, at=NOW)
    item = service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=(evidence(item, "founder_attestation"),), idempotency_key="formation-founder", correlation_id=CORRELATION, at=NOW)
    assert item.state is SetupState.WAITING_EXTERNAL
    with pytest.raises(AuthorityError):
        service.external_verified(TENANT, COMPANY, item.setup_item_id, actor=actor("agent", "setup_agent"), evidence=(evidence(item, "authority_confirmation", source="external_authority"),), idempotency_key="bad-external", correlation_id=CORRELATION, at=NOW)
    item = service.external_verified(TENANT, COMPANY, item.setup_item_id, actor=actor("external_authority", "authority_fictional"), evidence=(evidence(item, "authority_confirmation", source="external_authority"),), idempotency_key="good-external", correlation_id=CORRELATION, at=NOW)
    assert item.state is SetupState.COMPLETED
    history_count = len(service.repository.history(TENANT, COMPANY))
    replay = service.external_verified(TENANT, COMPANY, item.setup_item_id, actor=actor("external_authority", "authority_fictional"), evidence=(evidence(item, "authority_confirmation", source="external_authority"),), idempotency_key="good-external", correlation_id=CORRELATION, at=NOW)
    assert replay == item
    assert len(service.repository.history(TENANT, COMPANY)) == history_count


def test_dependency_change_invalidates_completed_dependent_and_re_evaluates():
    service, _ = billy_bob_setup()
    recovery = choose(service, item_for(service, "admin.account_recovery"))
    recovery = service.begin(TENANT, COMPANY, recovery.setup_item_id, actor=system_actor(), idempotency_key="recovery-begin", correlation_id=CORRELATION, at=NOW)
    recovery = service.founder_submitted(TENANT, COMPANY, recovery.setup_item_id, actor=actor(), evidence=(evidence(recovery, "founder_attestation"),), idempotency_key="recovery-founder", correlation_id=CORRELATION, at=NOW)
    assert recovery.state is SetupState.COMPLETED
    changed = service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 2, idempotency_key="invalidate-domain-v2", correlation_id=CORRELATION, at=NOW)
    assert len(changed) == 1
    assert changed[0].state is SetupState.INVALIDATED
    assert service.projection(TENANT, COMPANY).fully_set_eligible is False
    reselected = choose(service, changed[0], SetupMode.GUIDE_ME, suffix="reevaluate")
    assert reselected.state is SetupState.PLANNED
    assert reselected.stale_reason is None


def test_create_plan_and_mode_commands_are_idempotent_but_key_reuse_fails():
    service, original = billy_bob_setup()
    # Fixture's exact command replays without duplicate history.
    provenance = original[0].provenance[0]
    replay = service.create_plan(TENANT, COMPANY, "mobile_service", locality="Denton County, Texas", provenance=provenance, idempotency_key="billy-setup-plan-0001", correlation_id=CORRELATION, at=NOW)
    assert replay == original
    assert len(service.repository.history(TENANT, COMPANY)) == len(original)

    item = item_for(service, "admin.domain_account")
    first = choose(service, item, SetupMode.DO_IT, suffix="same-key")
    second = choose(service, item, SetupMode.DO_IT, suffix="same-key")
    assert first == second
    with pytest.raises(SetupAdminError):
        choose(service, item, SetupMode.SKIP, suffix="same-key")


def test_history_is_append_only_and_digest_bound():
    service, items = billy_bob_setup()
    choose(service, items[0])
    events = service.repository.history(TENANT, COMPANY)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert all(event.after_digest.startswith("sha256:") for event in events)
    with pytest.raises(SetupAdminError):
        service.repository.append_history(events[0])


def test_founder_action_template_validates_against_released_v2_contract():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    action = next(action for action in service.repository.list_actions(TENANT, COMPANY) if action.setup_item_id == item.setup_item_id)
    contracts = Path(__file__).resolve().parents[2] / "contracts"
    ContractValidator(contracts).validate("founder-action.v2.schema.json", action.to_contract())


def test_repository_returns_copies_so_callers_cannot_rewrite_history_or_state():
    service, items = billy_bob_setup()
    leaked = items[0]
    replace(leaked, state=SetupState.COMPLETED)
    stored = service.repository.get_item(TENANT, COMPANY, leaked.setup_item_id)
    assert stored.state is SetupState.UNDECIDED


def test_completed_founder_action_is_verified_and_not_pending():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="domain-begin", correlation_id=CORRELATION, at=NOW)
    supplied = (
        evidence(item, "founder_attestation"),
        evidence(item, "provider_receipt", source="provider"),
    )
    completed = service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=supplied, idempotency_key="domain-submit", correlation_id=CORRELATION, at=NOW)
    action = service.repository.list_actions(TENANT, COMPANY)[0]
    assert completed.state is SetupState.COMPLETED
    assert action.state == "verified"
    assert action.version == 3
    assert set(action.evidence_refs) == {entry.evidence_ref for entry in supplied}
    assert service.projection(TENANT, COMPANY).pending_founder_actions == ()
    lifecycle = [event for event in service.repository.history(TENANT, COMPANY) if event.setup_item_id == item.setup_item_id]
    assert all(event.founder_action_digest for event in lifecycle[1:])
    assert len({event.founder_action_digest for event in lifecycle[1:]}) == len(lifecycle[1:])


def test_new_plan_key_cannot_overwrite_progress_or_change_plan_identity():
    service, original = billy_bob_setup()
    item = choose(service, item_for(service, "admin.business_formation"), SetupMode.SKIP)
    history_count = len(service.repository.history(TENANT, COMPANY))
    replay = service.create_plan(TENANT, COMPANY, "mobile_service", locality="Denton County, Texas", provenance=original[0].provenance[0], idempotency_key="same-plan-new-key", correlation_id="retry", at=NOW)
    assert item_for(service, "admin.business_formation") == item
    assert len(service.repository.history(TENANT, COMPANY)) == history_count
    assert any(entry.state is SetupState.SKIPPED for entry in replay)
    changed_provenance = replace(original[0].provenance[0], source_ref="fixture://different")
    with pytest.raises(IllegalTransitionError):
        service.create_plan(TENANT, COMPANY, "mobile_service", locality="Denton County, Texas", provenance=changed_provenance, idempotency_key="different-plan", correlation_id="retry", at=NOW)


def test_mode_choice_requires_scoped_verified_actor_and_exact_effective_approval():
    service, _ = billy_bob_setup()
    item = item_for(service, "admin.domain_account")
    valid = approval_for(item, SetupMode.DO_IT)
    for bad_actor in (
        actor("agent", "agent_1"),
        actor(verified=False),
        actor(tenant="tenant_other"),
    ):
        with pytest.raises(AuthorityError):
            service.choose_mode(TENANT, COMPANY, item.setup_item_id, SetupMode.DO_IT, actor=bad_actor, approval=valid, idempotency_key=f"bad-{bad_actor.actor_id}-{bad_actor.tenant_id}-{bad_actor.verified}", correlation_id=CORRELATION, at=NOW)
    expired = replace(valid, expires_at=NOW)
    with pytest.raises(AuthorityError):
        service.choose_mode(TENANT, COMPANY, item.setup_item_id, SetupMode.DO_IT, actor=actor(), approval=expired, idempotency_key="expired", correlation_id=CORRELATION, at=NOW)
    wrong_digest = replace(valid, subject_digest="sha256:" + "0" * 64)
    with pytest.raises(AuthorityError):
        service.choose_mode(TENANT, COMPANY, item.setup_item_id, SetupMode.DO_IT, actor=actor(), approval=wrong_digest, idempotency_key="wrong-digest", correlation_id=CORRELATION, at=NOW)
    missing_amount = approval_for(item, SetupMode.DO_IT, amount_minor_units=None, currency=None)
    missing_amount = replace(missing_amount, amount_minor_units=None, currency=None, subject_digest=approval_subject_digest(item, SetupMode.DO_IT, missing_amount.target, None, None))
    with pytest.raises(AuthorityError):
        service.choose_mode(TENANT, COMPANY, item.setup_item_id, SetupMode.DO_IT, actor=actor(), approval=missing_amount, idempotency_key="missing-amount", correlation_id=CORRELATION, at=NOW)


def test_typed_evidence_must_be_verified_scoped_subject_bound_and_complete():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="typed-begin", correlation_id=CORRELATION, at=NOW)
    bad_sets = (
        (evidence(item, "founder_attestation", verified=False), evidence(item, "provider_receipt", source="provider")),
        (evidence(item, "founder_attestation", tenant="tenant_other"), evidence(item, "provider_receipt", source="provider")),
        (replace(evidence(item, "founder_attestation"), subject_id="setup_other"), evidence(item, "provider_receipt", source="provider")),
        (evidence(item, "founder_attestation"),),
    )
    for index, supplied in enumerate(bad_sets):
        with pytest.raises(MissingEvidenceError):
            service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=supplied, idempotency_key=f"bad-evidence-{index}", correlation_id=CORRELATION, at=NOW)
    assert item_for(service, "admin.domain_account").state is SetupState.WAITING_FOUNDER


def test_mutating_commands_replay_without_duplicate_history_and_reject_key_reuse():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.account_recovery"))
    first = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="retry-begin", correlation_id=CORRELATION, at=NOW)
    count = len(service.repository.history(TENANT, COMPANY))
    second = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="retry-begin", correlation_id=CORRELATION, at=NOW)
    assert second == first
    assert len(service.repository.history(TENANT, COMPANY)) == count
    supplied = (evidence(first, "founder_attestation"),)
    completed = service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=supplied, idempotency_key="retry-submit", correlation_id=CORRELATION, at=NOW)
    count = len(service.repository.history(TENANT, COMPANY))
    assert service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=supplied, idempotency_key="retry-submit", correlation_id=CORRELATION, at=NOW) == completed
    assert len(service.repository.history(TENANT, COMPANY)) == count
    with pytest.raises(SetupAdminError):
        service.founder_submitted(TENANT, COMPANY, item.setup_item_id, actor=actor(), evidence=(evidence(first, "founder_attestation", suffix="2"),), idempotency_key="retry-submit", correlation_id=CORRELATION, at=NOW)
    changed = service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 2, idempotency_key="retry-invalidate", correlation_id=CORRELATION, at=NOW)
    count = len(service.repository.history(TENANT, COMPANY))
    assert service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 2, idempotency_key="retry-invalidate", correlation_id=CORRELATION, at=NOW) == changed
    assert len(service.repository.history(TENANT, COMPANY)) == count


def test_invalidation_does_not_change_undecided_or_skipped_items():
    service, _ = billy_bob_setup()
    recovery = item_for(service, "admin.account_recovery")
    assert service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 2, idempotency_key="undecided-invalidate", correlation_id=CORRELATION, at=NOW) == ()
    skipped = choose(service, recovery, SetupMode.SKIP)
    assert service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 3, idempotency_key="skipped-invalidate", correlation_id=CORRELATION, at=NOW) == ()
    assert service.repository.get_item(TENANT, COMPANY, skipped.setup_item_id).state is SetupState.SKIPPED


def safe_service():
    catalog = RequirementCatalog((RequirementDefinition(
        "admin.safe_checklist",
        "Safe checklist",
        "Prepare a reversible checklist.",
        frozenset({"mobile_service"}),
        frozenset({"coordination"}),
        AuthorityGate.NONE,
        Criticality.ADVISORY,
        required_evidence_kinds=("system_receipt",),
    ),))
    service = SetupAdminService(InMemorySetupAdminRepository(), catalog)
    provenance = Provenance("founder", "fixture://safe", NOW, "founder", "party_billy")
    items = service.create_plan(TENANT, COMPANY, "mobile_service", locality=None, provenance=provenance, idempotency_key="safe-plan", correlation_id=CORRELATION, at=NOW)
    return service, items[0]


def test_do_it_and_guide_me_are_operationally_distinct_and_safe_ai_can_complete():
    do_service, do_item = safe_service()
    do_item = do_service.choose_mode(TENANT, COMPANY, do_item.setup_item_id, SetupMode.DO_IT, actor=actor(), idempotency_key="safe-do", correlation_id=CORRELATION, at=NOW)
    do_item = do_service.begin(TENANT, COMPANY, do_item.setup_item_id, actor=system_actor(), idempotency_key="safe-do-begin", correlation_id=CORRELATION, at=NOW)
    assert do_item.state is SetupState.IN_PROGRESS
    receipt = (evidence(do_item, "system_receipt", source="system"),)
    completed = do_service.complete_as_ai(TENANT, COMPANY, do_item.setup_item_id, evidence=receipt, idempotency_key="safe-complete", correlation_id=CORRELATION, at=NOW)
    assert completed.state is SetupState.COMPLETED
    assert do_service.complete_as_ai(TENANT, COMPANY, do_item.setup_item_id, evidence=receipt, idempotency_key="safe-complete", correlation_id=CORRELATION, at=NOW) == completed

    guide_service, guide_item = safe_service()
    guide_item = guide_service.choose_mode(TENANT, COMPANY, guide_item.setup_item_id, SetupMode.GUIDE_ME, actor=actor(), approval=approval_for(guide_item, SetupMode.GUIDE_ME), idempotency_key="safe-guide", correlation_id=CORRELATION, at=NOW)
    guide_item = guide_service.begin(TENANT, COMPANY, guide_item.setup_item_id, actor=system_actor("guide"), idempotency_key="safe-guide-begin", correlation_id=CORRELATION, at=NOW)
    assert guide_item.state is SetupState.WAITING_FOUNDER
    with pytest.raises(AuthorityError):
        guide_service.complete_as_ai(TENANT, COMPANY, guide_item.setup_item_id, evidence=receipt, idempotency_key="guide-ai", correlation_id=CORRELATION, at=NOW)


def test_high_risk_action_binds_approver_target_amount_expiry_and_approval():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    action = service.repository.list_actions(TENANT, COMPANY)[0]
    assert action.named_approver_id == "party_billy"
    assert action.named_approver_role == "founder"
    assert action.exact_target == "target_admin_domain_account"
    assert action.amount_minor_units == 1200 and action.currency == "USD"
    assert action.approval_ref == "approval_admin_domain_account"
    assert action.expires_at == NOW + timedelta(hours=1)
    contract = action.to_contract()
    assert contract["due_at"] == "2026-09-13T19:00:00Z"
    assert {entry["type"] for entry in contract["blocks"]} == {"readiness", "approval"}
    assert "USD 12.00" in " ".join(contract["instructions"])


def test_expired_action_cannot_be_submitted_even_if_it_was_once_approved():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="expiry-begin", correlation_id=CORRELATION, at=NOW)
    with pytest.raises(AuthorityError):
        service.founder_submitted(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=actor(),
            evidence=(evidence(item, "founder_attestation"), evidence(item, "provider_receipt", source="provider")),
            idempotency_key="expired-submit",
            correlation_id=CORRELATION,
            at=NOW + timedelta(hours=2),
        )


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("founder_action_id", None),
        ("approval_ref", "approval_unrelated"),
        ("approval_subject_digest", "sha256:" + "0" * 64),
        ("exact_target", "some-other-domain.example"),
        ("amount_minor_units", 999999),
        ("currency", "EUR"),
    ],
)
def test_high_risk_completion_rejects_generic_or_mismatched_receipt(field, bad_value):
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="bound-begin", correlation_id=CORRELATION, at=NOW)
    receipt = replace(evidence(item, "provider_receipt", source="provider"), **{field: bad_value})
    with pytest.raises(MissingEvidenceError, match="exact approved founder action"):
        service.founder_submitted(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=actor(),
            evidence=(evidence(item, "founder_attestation"), receipt),
            idempotency_key=f"bad-binding-{field}",
            correlation_id=CORRELATION,
            at=NOW,
        )
    assert item_for(service, "admin.domain_account").state is SetupState.WAITING_FOUNDER


def test_completion_evidence_requires_verified_source_authorization():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="source-begin", correlation_id=CORRELATION, at=NOW)
    receipt = evidence(item, "provider_receipt", source="provider")
    forged = replace(receipt, authorized_actor=replace(receipt.authorized_actor, verified=False))
    with pytest.raises(AuthorityError, match="not verified"):
        service.founder_submitted(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=actor(),
            evidence=(evidence(item, "founder_attestation"), forged),
            idempotency_key="forged-provider",
            correlation_id=CORRELATION,
            at=NOW,
        )


def test_begin_requires_verified_scoped_trusted_actor_and_cannot_spoof_audit():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    before_history = service.repository.history(TENANT, COMPANY)
    for index, untrusted in enumerate((system_actor(verified=False), actor("agent", "spoofed"), system_actor("wrong-scope", verified=True))):
        if index == 2:
            untrusted = replace(untrusted, tenant_id="tenant_other")
        with pytest.raises(AuthorityError):
            service.begin(
                TENANT,
                COMPANY,
                item.setup_item_id,
                actor=untrusted,
                idempotency_key=f"untrusted-begin-{index}",
                correlation_id=CORRELATION,
                at=NOW,
            )
    assert item_for(service, "admin.domain_account").state is SetupState.PLANNED
    assert service.repository.history(TENANT, COMPANY) == before_history


def test_founder_action_rejects_same_approver_id_under_a_different_role():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.domain_account"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="role-bound-begin", correlation_id=CORRELATION, at=NOW)
    supplied = (
        evidence(item, "founder_attestation"),
        evidence(item, "provider_receipt", source="provider"),
    )

    with pytest.raises(AuthorityError, match="named approver"):
        service.founder_submitted(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=actor("authorized_human", "party_billy"),
            evidence=supplied,
            idempotency_key="role-confused-submit",
            correlation_id=CORRELATION,
            at=NOW,
        )

    assert item_for(service, "admin.domain_account").state is SetupState.WAITING_FOUNDER

    action = service.repository.get_action(TENANT, COMPANY, item.current_founder_action_id)
    with pytest.raises(SetupAdminError, match="approval binding are immutable"):
        service.repository.save_action(
            action.with_update(named_approver_role="authorized_human")
        )


def test_external_verification_rejects_same_actor_id_under_a_different_role():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.business_formation"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="external-role-begin", correlation_id=CORRELATION, at=NOW)
    item = service.founder_submitted(
        TENANT,
        COMPANY,
        item.setup_item_id,
        actor=actor(),
        evidence=(evidence(item, "founder_attestation"),),
        idempotency_key="external-role-founder",
        correlation_id=CORRELATION,
        at=NOW,
    )
    provider_evidence = evidence(item, "authority_confirmation", source="provider")

    with pytest.raises(MissingEvidenceError, match="exact submitting actor"):
        service.external_verified(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=actor("external_authority", "provider_fictional"),
            evidence=(provider_evidence,),
            idempotency_key="external-role-confused",
            correlation_id=CORRELATION,
            at=NOW,
        )

    assert item_for(service, "admin.business_formation").state is SetupState.WAITING_EXTERNAL


def test_external_verification_rejects_changed_actor_verification_identity():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.business_formation"))
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="external-identity-begin", correlation_id=CORRELATION, at=NOW)
    item = service.founder_submitted(
        TENANT,
        COMPANY,
        item.setup_item_id,
        actor=actor(),
        evidence=(evidence(item, "founder_attestation"),),
        idempotency_key="external-identity-founder",
        correlation_id=CORRELATION,
        at=NOW,
    )
    authority_evidence = evidence(item, "authority_confirmation", source="external_authority")
    changed_identity = replace(
        authority_evidence.authorized_actor,
        verification_ref="identity_authority_fictional_reissued",
    )

    with pytest.raises(MissingEvidenceError, match="exact submitting actor"):
        service.external_verified(
            TENANT,
            COMPANY,
            item.setup_item_id,
            actor=changed_identity,
            evidence=(authority_evidence,),
            idempotency_key="external-identity-confused",
            correlation_id=CORRELATION,
            at=NOW,
        )


def test_invalidation_and_reselection_preserve_action_generations_and_all_versions():
    service, _ = billy_bob_setup()
    item = choose(service, item_for(service, "admin.account_recovery"))
    first_action_id = item.current_founder_action_id
    item = service.begin(TENANT, COMPANY, item.setup_item_id, actor=system_actor(), idempotency_key="history-begin", correlation_id=CORRELATION, at=NOW)
    item = service.founder_submitted(
        TENANT,
        COMPANY,
        item.setup_item_id,
        actor=actor(),
        evidence=(evidence(item, "founder_attestation"),),
        idempotency_key="history-submit",
        correlation_id=CORRELATION,
        at=NOW,
    )
    service.invalidate_dependency(TENANT, COMPANY, "admin.domain_account", 2, idempotency_key="history-invalidate", correlation_id=CORRELATION, at=NOW)
    old_versions = service.repository.action_history(TENANT, COMPANY, first_action_id)
    assert [entry.state for entry in old_versions] == ["required", "in_progress", "verified", "expired"]
    assert [entry.version for entry in old_versions] == [1, 2, 3, 4]
    assert old_versions[2].evidence_refs == ("evidence_founder_attestation_1",)

    invalidated = item_for(service, "admin.account_recovery")
    replacement = choose(service, invalidated, SetupMode.DO_IT, suffix="new-generation")
    assert replacement.current_founder_action_id != first_action_id
    assert replacement.current_founder_action_id.endswith("_g2")
    assert service.repository.get_action(TENANT, COMPANY, first_action_id).state == "expired"
    assert service.repository.action_history(TENANT, COMPANY, first_action_id) == old_versions
    new_action = service.repository.get_action(TENANT, COMPANY, replacement.current_founder_action_id)
    assert new_action.state == "required" and new_action.version == 1 and new_action.evidence_refs == ()
