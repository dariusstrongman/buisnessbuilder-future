from __future__ import annotations

from datetime import datetime

from .catalog import RequirementCatalog
from .models import (
    ActorBinding,
    ApprovalBinding,
    AuthorityGate,
    Criticality,
    EvidenceReference,
    FounderAction,
    Provenance,
    SetupHistoryEvent,
    SetupItem,
    SetupMode,
    SetupProjection,
    SetupState,
    digest,
    utc_now,
)
from .repository import InMemorySetupAdminRepository, NotFoundError, SetupAdminError


class IllegalTransitionError(SetupAdminError):
    pass


class AuthorityError(SetupAdminError):
    pass


class MissingEvidenceError(SetupAdminError):
    pass


ACTIVE = {SetupState.PLANNED, SetupState.IN_PROGRESS, SetupState.WAITING_FOUNDER, SetupState.WAITING_EXTERNAL}
HUMAN_ONLY_ACTIONS = {"identity", "sign", "file", "purchase", "accept_terms", "open_account", "choose_insurance", "attest_license", "authorize_spend", "hire"}
FOUNDER_ROLES = {"founder", "authorized_human"}
EXTERNAL_EVIDENCE_SOURCES = {"external_authority", "provider", "authoritative_source"}


def approval_subject_digest(
    item: SetupItem,
    mode: SetupMode,
    target: str,
    amount_minor_units: int | None,
    currency: str | None,
) -> str:
    return digest(
        {
            "setup_item_id": item.setup_item_id,
            "requirement_id": item.requirement_id,
            "mode": mode.value,
            "target": target,
            "amount_minor_units": amount_minor_units,
            "currency": currency,
        }
    )


class SetupAdminService:
    def __init__(self, repository: InMemorySetupAdminRepository, catalog: RequirementCatalog) -> None:
        self.repository = repository
        self.catalog = catalog

    def create_plan(
        self,
        tenant_id: str,
        company_id: str,
        archetype: str,
        *,
        locality: str | None,
        provenance: Provenance,
        idempotency_key: str,
        correlation_id: str,
        at: datetime | None = None,
    ) -> tuple[SetupItem, ...]:
        now = at or utc_now()
        command = digest({"op": "create_plan", "archetype": archetype, "locality": locality, "provenance": provenance.to_dict()})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        definitions = self.catalog.for_company(archetype, locality=locality)
        existing = self.repository.list_items(tenant_id, company_id)
        if existing:
            if self.repository.plan_digest(tenant_id, company_id) != command:
                raise IllegalTransitionError("a different setup plan already exists")
            expected = {definition.requirement_id for definition in definitions}
            actual = {item.requirement_id for item in existing}
            if actual != expected:
                raise IllegalTransitionError("a different or partial setup plan already exists")
            result = tuple(existing)
            self.repository.remember(tenant_id, company_id, idempotency_key, command, result)
            return result
        items: list[SetupItem] = []
        for definition in definitions:
            item = SetupItem(
                setup_item_id=f"setup_{definition.requirement_id.replace('.', '_')}",
                tenant_id=tenant_id,
                company_id=company_id,
                requirement_id=definition.requirement_id,
                provenance=(provenance,),
                created_at=now,
                updated_at=now,
            )
            self.repository.create_item(item)
            self._history(item, None, "setup.created", "requirement matched company archetype", correlation_id, provenance.actor_type, provenance.actor_id, now)
            items.append(item)
        result = tuple(items)
        self.repository.remember_plan(tenant_id, company_id, command)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, result)
        return result

    def choose_mode(
        self,
        tenant_id: str,
        company_id: str,
        setup_item_id: str,
        mode: SetupMode,
        *,
        actor: ActorBinding,
        approval: ApprovalBinding | None = None,
        idempotency_key: str,
        correlation_id: str,
        at: datetime | None = None,
    ) -> SetupItem:
        now = at or utc_now()
        self._require_actor(actor, tenant_id, company_id, FOUNDER_ROLES)
        command = digest({"op": "choose_mode", "item": setup_item_id, "mode": mode.value, "actor": actor.snapshot(), "approval": approval.snapshot() if approval else None})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        before = self.repository.get_item(tenant_id, company_id, setup_item_id)
        if before.state not in {SetupState.UNDECIDED, SetupState.INVALIDATED}:
            raise IllegalTransitionError(f"cannot choose mode from {before.state.value}")
        definition = self.catalog.get(before.requirement_id)
        if mode is SetupMode.SKIP:
            after = before.with_update(mode=mode, state=SetupState.SKIPPED, stale_reason=None, evidence_refs=(), current_founder_action_id=None, at=now)
        else:
            # Invalidation deliberately discards old evidence. Re-evaluation must
            # traverse the founder/external gates with fresh receipts.
            action_id = self._next_action_id(before) if definition.authority_gate is not AuthorityGate.NONE or mode is SetupMode.GUIDE_ME else None
            after = before.with_update(mode=mode, state=SetupState.PLANNED, stale_reason=None, evidence_refs=(), current_founder_action_id=action_id, at=now)
            if definition.authority_gate is not AuthorityGate.NONE or mode is SetupMode.GUIDE_ME:
                if approval is None:
                    raise AuthorityError("founder work requires a bound Approval v2 grant")
                self._require_approval(approval, actor, before, mode, definition, now)
                self.repository.save_action(self._action_for(after, definition, approval, action_id, now))
        self.repository.save_item(after)
        self._history(after, before, "setup.mode_chosen", f"verified {actor.actor_type} selected {mode.value}", correlation_id, actor.actor_type, actor.actor_id, now)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, after)
        return after

    def begin(self, tenant_id: str, company_id: str, setup_item_id: str, *, actor: ActorBinding, idempotency_key: str, correlation_id: str, at: datetime | None = None) -> SetupItem:
        now = at or utc_now()
        self._require_actor(actor, tenant_id, company_id, {"system", "authorized_agent", "founder", "authorized_human"})
        command = digest({"op": "begin", "item": setup_item_id, "actor": actor.snapshot()})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        before = self.repository.get_item(tenant_id, company_id, setup_item_id)
        if before.state is not SetupState.PLANNED:
            raise IllegalTransitionError(f"cannot begin from {before.state.value}")
        definition = self.catalog.get(before.requirement_id)
        if definition.authority_gate is AuthorityGate.EXTERNAL:
            state = SetupState.WAITING_EXTERNAL
        elif definition.authority_gate is AuthorityGate.NONE and before.mode is SetupMode.DO_IT:
            state = SetupState.IN_PROGRESS
        else:
            state = SetupState.WAITING_FOUNDER
        after = before.with_update(state=state, at=now)
        self.repository.save_item(after)
        self._update_action_state(after, "in_progress")
        reason = "system preparation and coordination started" if before.mode is SetupMode.DO_IT else "founder guidance started"
        self._history(after, before, "setup.started", reason, correlation_id, actor.actor_type, actor.actor_id, now)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, after)
        return after

    def founder_submitted(self, tenant_id: str, company_id: str, setup_item_id: str, *, actor: ActorBinding, evidence: tuple[EvidenceReference, ...], idempotency_key: str, correlation_id: str, at: datetime | None = None) -> SetupItem:
        self._require_actor(actor, tenant_id, company_id, FOUNDER_ROLES)
        now = at or utc_now()
        command = digest({"op": "founder_submitted", "item": setup_item_id, "actor": actor.snapshot(), "evidence": [item.snapshot() for item in evidence]})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        before = self.repository.get_item(tenant_id, company_id, setup_item_id)
        if before.state is not SetupState.WAITING_FOUNDER:
            raise IllegalTransitionError(f"cannot submit from {before.state.value}")
        definition = self.catalog.get(before.requirement_id)
        action = self._require_current_action(before, actor, now)
        self._validate_evidence(evidence, before, tenant_id, company_id)
        if not any(
            item.source_type == actor.actor_type
            and item.source_id == actor.actor_id
            and item.authorized_actor == actor
            and item.kind in definition.required_evidence_kinds
            for item in evidence
        ):
            raise MissingEvidenceError("founder submission requires evidence authorized to the exact submitting actor")
        refs = tuple(dict.fromkeys((*before.evidence_refs, *(item.evidence_ref for item in evidence))))
        state = SetupState.WAITING_EXTERNAL if definition.authority_gate in {AuthorityGate.EXTERNAL, AuthorityGate.FOUNDER_AND_EXTERNAL} else SetupState.COMPLETED
        if state is SetupState.COMPLETED:
            self._require_all_evidence(definition.required_evidence_kinds, evidence)
        self._require_bound_completion_evidence(action, definition.required_evidence_kinds, evidence)
        after = before.with_update(state=state, evidence_refs=refs, at=now)
        self.repository.save_item(after)
        self._remember_evidence(evidence)
        self._update_action_state(after, "submitted" if state is SetupState.WAITING_EXTERNAL else "verified", refs)
        self._history(after, before, "setup.founder_submitted", "verified founder supplied typed evidence", correlation_id, actor.actor_type, actor.actor_id, now)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, after)
        return after

    def external_verified(self, tenant_id: str, company_id: str, setup_item_id: str, *, actor: ActorBinding, evidence: tuple[EvidenceReference, ...], idempotency_key: str, correlation_id: str, at: datetime | None = None) -> SetupItem:
        self._require_actor(actor, tenant_id, company_id, EXTERNAL_EVIDENCE_SOURCES)
        now = at or utc_now()
        command = digest({"op": "external_verified", "item": setup_item_id, "actor": actor.snapshot(), "evidence": [item.snapshot() for item in evidence]})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        before = self.repository.get_item(tenant_id, company_id, setup_item_id)
        if before.state is not SetupState.WAITING_EXTERNAL:
            raise IllegalTransitionError(f"cannot externally verify from {before.state.value}")
        definition = self.catalog.get(before.requirement_id)
        action = self._current_action(before)
        self._validate_evidence(evidence, before, tenant_id, company_id)
        if not all(
            item.source_type == actor.actor_type
            and item.source_id == actor.actor_id
            and item.authorized_actor == actor
            for item in evidence
        ):
            raise MissingEvidenceError("external verification evidence must be authorized to the exact submitting actor")
        existing_kinds = self.repository.evidence_kinds(tenant_id, company_id, before.evidence_refs)
        supplied_kinds = {item.kind for item in evidence}
        missing = set(definition.required_evidence_kinds) - existing_kinds - supplied_kinds
        if missing:
            raise MissingEvidenceError(f"missing required evidence kinds: {sorted(missing)}")
        if action is not None:
            self._require_bound_completion_evidence(action, definition.required_evidence_kinds, evidence)
        refs = tuple(dict.fromkeys((*before.evidence_refs, *(item.evidence_ref for item in evidence))))
        after = before.with_update(state=SetupState.COMPLETED, evidence_refs=refs, at=now)
        self.repository.save_item(after)
        self._remember_evidence(evidence)
        self._update_action_state(after, "verified", refs)
        self._history(after, before, "setup.external_verified", "bound external authority evidence verified", correlation_id, actor.actor_type, actor.actor_id, now)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, after)
        return after

    def complete_as_ai(self, tenant_id: str, company_id: str, setup_item_id: str, *, evidence: tuple[EvidenceReference, ...], idempotency_key: str, correlation_id: str, at: datetime | None = None) -> SetupItem:
        now = at or utc_now()
        command = digest({"op": "complete_as_ai", "item": setup_item_id, "evidence": [item.snapshot() for item in evidence]})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        item = self.repository.get_item(tenant_id, company_id, setup_item_id)
        definition = self.catalog.get(item.requirement_id)
        if definition.authority_gate is not AuthorityGate.NONE or definition.founder_action_type in HUMAN_ONLY_ACTIONS:
            raise AuthorityError("AI cannot sign, file, pay, purchase, accept terms, choose insurance, or attest")
        if item.mode is not SetupMode.DO_IT:
            raise AuthorityError("AI completion is available only for Do It work")
        if item.state is not SetupState.IN_PROGRESS:
            raise IllegalTransitionError(f"cannot complete from {item.state.value}")
        self._validate_evidence(evidence, item, tenant_id, company_id)
        self._require_all_evidence(definition.required_evidence_kinds, evidence)
        refs = tuple(dict.fromkeys((*item.evidence_refs, *(entry.evidence_ref for entry in evidence))))
        after = item.with_update(state=SetupState.COMPLETED, evidence_refs=refs, at=now)
        self.repository.save_item(after)
        self._remember_evidence(evidence)
        self._history(after, item, "setup.automation_completed", "safe reversible work completed with typed evidence", correlation_id, "system", "setup_admin", now)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, after)
        return after

    def invalidate_dependency(self, tenant_id: str, company_id: str, dependency_id: str, new_version: int, *, idempotency_key: str, correlation_id: str, at: datetime | None = None) -> tuple[SetupItem, ...]:
        now = at or utc_now()
        command = digest({"op": "invalidate_dependency", "dependency_id": dependency_id, "new_version": new_version})
        replay = self.repository.replay(tenant_id, company_id, idempotency_key, command)
        if replay is not None:
            return replay  # type: ignore[return-value]
        changed: list[SetupItem] = []
        for before in self.repository.list_items(tenant_id, company_id):
            definition = self.catalog.get(before.requirement_id)
            if dependency_id not in definition.dependency_ids:
                continue
            if before.state not in ACTIVE | {SetupState.COMPLETED}:
                continue
            versions = dict(before.dependency_versions)
            if versions.get(dependency_id, 0) >= new_version:
                continue
            versions[dependency_id] = new_version
            after = before.with_update(state=SetupState.INVALIDATED, dependency_versions=tuple(sorted(versions.items())), stale_reason=f"dependency {dependency_id} changed to version {new_version}", at=now)
            self.repository.save_item(after)
            self._update_action_state(after, "expired")
            self._history(after, before, "setup.invalidated", after.stale_reason or "dependency changed", correlation_id, "system", "setup_admin", now)
            changed.append(after)
        result = tuple(changed)
        self.repository.remember(tenant_id, company_id, idempotency_key, command, result)
        return result

    def projection(self, tenant_id: str, company_id: str) -> SetupProjection:
        blockers: list[str] = []
        consequences: list[str] = []
        for item in self.repository.list_items(tenant_id, company_id):
            definition = self.catalog.get(item.requirement_id)
            if item.state is SetupState.SKIPPED:
                if definition.skip_consequence:
                    consequences.append(definition.skip_consequence)
                if definition.blocks_fully_set_when_skipped:
                    blockers.append(f"{definition.requirement_id}: skipped")
            elif definition.criticality in {Criticality.CRITICAL, Criticality.MATERIAL} and item.state is not SetupState.COMPLETED:
                blockers.append(f"{definition.requirement_id}: {item.state.value}")
        pending = tuple(action for action in self.repository.list_actions(tenant_id, company_id) if action.state not in {"verified", "declined", "expired", "waived_noncritical"})
        return SetupProjection(tenant_id, company_id, not blockers, tuple(blockers), tuple(consequences), pending)

    @staticmethod
    def _action_for(item: SetupItem, definition: object, approval: ApprovalBinding, action_id: str | None, now: datetime) -> FounderAction:
        # Definition is internal catalog data; emitted shape conforms to FounderAction v2.
        action_type = getattr(definition, "founder_action_type") or "other"
        risk = "high" if action_type in HUMAN_ONLY_ACTIONS else "medium"
        amount_preview = (
            f"Maximum authorized charge: {approval.currency} {approval.amount_minor_units / 100:.2f}."
            if approval.amount_minor_units is not None and approval.currency
            else "No payment is authorized by this action."
        )
        mode_preview = (
            "The system may prepare and coordinate reversible prerequisites; the named approver performs this controlled action."
            if item.mode is SetupMode.DO_IT
            else "The system provides instructions only; the named approver performs and records this controlled action."
        )
        return FounderAction(
            founder_action_id=action_id or f"founder_action_{item.requirement_id.replace('.', '_')}",
            tenant_id=item.tenant_id,
            company_id=item.company_id,
            setup_item_id=item.setup_item_id,
            action_type=action_type,
            title=getattr(definition, "title"),
            reason=getattr(definition, "description"),
            instructions=(
                mode_preview,
                f"Exact target: {approval.target}.",
                amount_preview,
                f"Named approver: {approval.approver_id}; approval expires {approval.expires_at.isoformat()}.",
                "Review the neutral checklist and current provider or authority information.",
                "Complete any identity, signature, attestation, filing, terms, or payment step yourself.",
                "Return only a receipt or evidence reference; never provide credentials or payment data.",
            ),
            risk=risk,
            irreversible=action_type in HUMAN_ONLY_ACTIONS,
            required_evidence_kinds=getattr(definition, "required_evidence_kinds"),
            blocks=getattr(definition, "blocks"),
            state="required",
            created_at=now,
            named_approver_id=approval.approver_id,
            named_approver_role=approval.approver_role,
            exact_target=approval.target,
            approval_ref=approval.approval_ref,
            approval_subject_digest=approval.subject_digest,
            expires_at=approval.expires_at,
            amount_minor_units=approval.amount_minor_units,
            currency=approval.currency,
        )

    @staticmethod
    def _require_actor(actor: ActorBinding, tenant_id: str, company_id: str, allowed_roles: set[str]) -> None:
        if not actor.verified or not actor.verification_ref:
            raise AuthorityError("actor identity is not verified")
        if actor.tenant_id != tenant_id or actor.company_id != company_id:
            raise AuthorityError("actor identity is outside tenant/company scope")
        if actor.actor_type not in allowed_roles:
            raise AuthorityError(f"action requires one of these roles: {sorted(allowed_roles)}")

    @staticmethod
    def _require_approval(
        approval: ApprovalBinding,
        actor: ActorBinding,
        item: SetupItem,
        mode: SetupMode,
        definition: object,
        now: datetime,
    ) -> None:
        if approval.tenant_id != item.tenant_id or approval.company_id != item.company_id:
            raise AuthorityError("approval is outside tenant/company scope")
        if approval.expires_at.tzinfo is None or approval.state != "granted" or approval.expires_at <= now:
            raise AuthorityError("approval is not effective")
        if approval.approver_id != actor.actor_id or approval.approver_role != actor.actor_type:
            raise AuthorityError("approval is not bound to the verified actor")
        expected = approval_subject_digest(item, mode, approval.target, approval.amount_minor_units, approval.currency)
        if approval.subject_digest != expected:
            raise AuthorityError("approval subject digest does not match the exact setup action")
        if not approval.target.strip():
            raise AuthorityError("approval requires an exact target")
        if not approval.approval_ref:
            raise AuthorityError("approval requires a public Approval v2 reference")
        action_type = getattr(definition, "founder_action_type")
        if action_type in {"purchase", "authorize_spend"}:
            if approval.amount_minor_units is None or approval.amount_minor_units < 0 or not approval.currency or len(approval.currency) != 3 or not approval.currency.isupper():
                raise AuthorityError("purchase/spend approval requires an exact amount and currency")

    def _validate_evidence(
        self,
        evidence: tuple[EvidenceReference, ...],
        item: SetupItem,
        tenant_id: str,
        company_id: str,
    ) -> None:
        if not evidence:
            raise MissingEvidenceError("typed evidence is required")
        for entry in evidence:
            if entry.tenant_id != tenant_id or entry.company_id != company_id:
                raise MissingEvidenceError("evidence is outside tenant/company scope")
            if entry.subject_id != item.setup_item_id:
                raise MissingEvidenceError("evidence is bound to a different setup item")
            if entry.captured_at.tzinfo is None or not entry.verified or not entry.evidence_ref or not entry.kind or not entry.source_id or not entry.verification_ref:
                raise MissingEvidenceError("evidence must be verified, typed, and reference stored content")
            evidence_actor = entry.authorized_actor
            if evidence_actor is None:
                raise MissingEvidenceError("evidence requires a verified source authorization")
            self._require_actor(evidence_actor, tenant_id, company_id, {entry.source_type})
            if evidence_actor.actor_id != entry.source_id:
                raise MissingEvidenceError("evidence source does not match its authorized actor")
            try:
                existing = self.repository.get_evidence(tenant_id, company_id, entry.evidence_ref)
            except NotFoundError:
                continue
            if existing != entry:
                raise MissingEvidenceError("evidence reference is already bound to different metadata")

    @staticmethod
    def _require_all_evidence(required: tuple[str, ...], evidence: tuple[EvidenceReference, ...]) -> None:
        missing = set(required) - {item.kind for item in evidence}
        if missing:
            raise MissingEvidenceError(f"missing required evidence kinds: {sorted(missing)}")

    def _remember_evidence(self, evidence: tuple[EvidenceReference, ...]) -> None:
        for item in evidence:
            self.repository.save_evidence(item)

    def _update_action_state(self, item: SetupItem, state: str, evidence_refs: tuple[str, ...] = ()) -> None:
        action_id = item.current_founder_action_id
        if action_id is None:
            return
        try:
            action = self.repository.get_action(item.tenant_id, item.company_id, action_id)
        except NotFoundError:
            return
        refs = tuple(dict.fromkeys((*action.evidence_refs, *evidence_refs)))
        self.repository.save_action(action.with_update(state=state, evidence_refs=refs))

    def _require_current_action(self, item: SetupItem, actor: ActorBinding, now: datetime) -> FounderAction:
        action_id = item.current_founder_action_id
        if action_id is None:
            raise AuthorityError("setup item has no current founder action")
        try:
            action = self.repository.get_action(item.tenant_id, item.company_id, action_id)
        except NotFoundError as exc:
            raise AuthorityError("founder action has no bound approval") from exc
        if (action.named_approver_id, action.named_approver_role) != (actor.actor_id, actor.actor_type):
            raise AuthorityError("actor is not the action's named approver")
        if action.expires_at <= now:
            raise AuthorityError("founder action approval has expired")
        return action

    def _current_action(self, item: SetupItem) -> FounderAction | None:
        if item.current_founder_action_id is None:
            return None
        try:
            return self.repository.get_action(item.tenant_id, item.company_id, item.current_founder_action_id)
        except NotFoundError:
            return None

    def _next_action_id(self, item: SetupItem) -> str:
        base = f"founder_action_{item.requirement_id.replace('.', '_')}"
        existing = {action.founder_action_id for action in self.repository.list_actions(item.tenant_id, item.company_id)}
        if base not in existing:
            return base
        generation = 2
        while f"{base}_g{generation}" in existing:
            generation += 1
        return f"{base}_g{generation}"

    @staticmethod
    def _require_bound_completion_evidence(action: FounderAction, required_kinds: tuple[str, ...], evidence: tuple[EvidenceReference, ...]) -> None:
        for entry in evidence:
            if entry.kind not in required_kinds:
                continue
            expected = (
                action.founder_action_id,
                action.approval_ref,
                action.approval_subject_digest,
                action.exact_target,
                action.amount_minor_units,
                action.currency,
            )
            actual = (
                entry.founder_action_id,
                entry.approval_ref,
                entry.approval_subject_digest,
                entry.exact_target,
                entry.amount_minor_units,
                entry.currency,
            )
            if actual != expected:
                raise MissingEvidenceError("completion evidence is not bound to the exact approved founder action")

    def _history(self, after: SetupItem, before: SetupItem | None, action: str, reason: str, correlation_id: str, actor_type: str, actor_id: str, at: datetime) -> None:
        existing = self.repository.history(after.tenant_id, after.company_id)
        action_id = after.current_founder_action_id
        try:
            if action_id is None:
                raise NotFoundError("no current founder action")
            founder_action = self.repository.get_action(after.tenant_id, after.company_id, action_id)
            founder_action_digest = digest(founder_action.snapshot())
        except NotFoundError:
            founder_action_digest = None
        self.repository.append_history(
            SetupHistoryEvent(
                event_id=f"setup_history_{len(existing) + 1:04d}",
                tenant_id=after.tenant_id,
                company_id=after.company_id,
                setup_item_id=after.setup_item_id,
                action=action,
                occurred_at=at,
                actor_type=actor_type,
                actor_id=actor_id,
                before_digest=digest(before.snapshot()) if before else None,
                after_digest=digest(after.snapshot()),
                reason=reason,
                correlation_id=correlation_id,
                sequence=len(existing) + 1,
                founder_action_digest=founder_action_digest,
            )
        )
