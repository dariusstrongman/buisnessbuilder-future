from __future__ import annotations

from copy import deepcopy

from .models import EvidenceReference, FounderAction, SetupHistoryEvent, SetupItem


class SetupAdminError(RuntimeError):
    pass


class NotFoundError(SetupAdminError):
    pass


class ScopeError(SetupAdminError):
    pass


class InMemorySetupAdminRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], SetupItem] = {}
        self._actions: dict[tuple[str, str, str], FounderAction] = {}
        self._action_versions: dict[tuple[str, str, str], list[FounderAction]] = {}
        self._history: dict[tuple[str, str], list[SetupHistoryEvent]] = {}
        self._evidence: dict[tuple[str, str, str], EvidenceReference] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, object]] = {}
        self._plans: dict[tuple[str, str], str] = {}

    def save_item(self, item: SetupItem) -> SetupItem:
        key = (item.tenant_id, item.company_id, item.setup_item_id)
        self._items[key] = deepcopy(item)
        return deepcopy(item)

    def create_item(self, item: SetupItem) -> SetupItem:
        key = (item.tenant_id, item.company_id, item.setup_item_id)
        if key in self._items:
            raise SetupAdminError("setup item already exists")
        self._items[key] = deepcopy(item)
        return deepcopy(item)

    def get_item(self, tenant_id: str, company_id: str, setup_item_id: str) -> SetupItem:
        try:
            return deepcopy(self._items[(tenant_id, company_id, setup_item_id)])
        except KeyError as exc:
            # Do not reveal whether another tenant owns the opaque identifier.
            raise NotFoundError("setup item not found in tenant/company scope") from exc

    def list_items(self, tenant_id: str, company_id: str) -> tuple[SetupItem, ...]:
        return tuple(
            deepcopy(item)
            for (tenant, company, _), item in self._items.items()
            if tenant == tenant_id and company == company_id
        )

    def save_action(self, action: FounderAction) -> FounderAction:
        key = (action.tenant_id, action.company_id, action.founder_action_id)
        versions = self._action_versions.setdefault(key, [])
        if versions:
            prior = versions[-1]
            if action.version != prior.version + 1:
                raise SetupAdminError("founder action versions must advance exactly once")
            immutable_before = (
                prior.tenant_id, prior.company_id, prior.founder_action_id,
                prior.setup_item_id, prior.action_type, prior.created_at,
                prior.named_approver_id, prior.named_approver_role,
                prior.exact_target, prior.approval_ref,
                prior.approval_subject_digest, prior.expires_at,
                prior.amount_minor_units, prior.currency,
            )
            immutable_after = (
                action.tenant_id, action.company_id, action.founder_action_id,
                action.setup_item_id, action.action_type, action.created_at,
                action.named_approver_id, action.named_approver_role,
                action.exact_target, action.approval_ref,
                action.approval_subject_digest, action.expires_at,
                action.amount_minor_units, action.currency,
            )
            if immutable_after != immutable_before:
                raise SetupAdminError("founder action identity and approval binding are immutable")
            if not set(prior.evidence_refs).issubset(action.evidence_refs):
                raise SetupAdminError("founder action evidence receipts are append-only")
        elif action.version != 1:
            raise SetupAdminError("new founder actions must start at version 1")
        versions.append(deepcopy(action))
        self._actions[key] = deepcopy(action)
        return deepcopy(action)

    def get_action(self, tenant_id: str, company_id: str, founder_action_id: str) -> FounderAction:
        try:
            return deepcopy(self._actions[(tenant_id, company_id, founder_action_id)])
        except KeyError as exc:
            raise NotFoundError("founder action not found in tenant/company scope") from exc

    def list_actions(self, tenant_id: str, company_id: str) -> tuple[FounderAction, ...]:
        return tuple(
            deepcopy(action)
            for (tenant, company, _), action in self._actions.items()
            if tenant == tenant_id and company == company_id
        )

    def action_history(self, tenant_id: str, company_id: str, founder_action_id: str) -> tuple[FounderAction, ...]:
        key = (tenant_id, company_id, founder_action_id)
        if key not in self._action_versions:
            raise NotFoundError("founder action not found in tenant/company scope")
        return tuple(deepcopy(self._action_versions[key]))

    def save_evidence(self, evidence: EvidenceReference) -> EvidenceReference:
        key = (evidence.tenant_id, evidence.company_id, evidence.evidence_ref)
        existing = self._evidence.get(key)
        if existing is not None and existing != evidence:
            raise SetupAdminError("evidence reference cannot be rebound")
        self._evidence[key] = deepcopy(evidence)
        return deepcopy(evidence)

    def get_evidence(self, tenant_id: str, company_id: str, evidence_ref: str) -> EvidenceReference:
        try:
            return deepcopy(self._evidence[(tenant_id, company_id, evidence_ref)])
        except KeyError as exc:
            raise NotFoundError("evidence not found in tenant/company scope") from exc

    def evidence_kinds(self, tenant_id: str, company_id: str, evidence_refs: tuple[str, ...]) -> set[str]:
        return {self.get_evidence(tenant_id, company_id, evidence_ref).kind for evidence_ref in evidence_refs}

    def append_history(self, event: SetupHistoryEvent) -> None:
        key = (event.tenant_id, event.company_id)
        events = self._history.setdefault(key, [])
        if any(existing.event_id == event.event_id for existing in events):
            raise SetupAdminError("history event IDs are append-only and unique")
        events.append(deepcopy(event))

    def history(self, tenant_id: str, company_id: str) -> tuple[SetupHistoryEvent, ...]:
        return tuple(deepcopy(self._history.get((tenant_id, company_id), [])))

    def replay(self, tenant_id: str, company_id: str, key: str, command_digest: str) -> object | None:
        stored = self._idempotency.get((tenant_id, company_id, key))
        if stored is None:
            return None
        if stored[0] != command_digest:
            raise SetupAdminError("idempotency key was reused with a different command")
        return deepcopy(stored[1])

    def remember(self, tenant_id: str, company_id: str, key: str, command_digest: str, result: object) -> None:
        self._idempotency[(tenant_id, company_id, key)] = (command_digest, deepcopy(result))

    def plan_digest(self, tenant_id: str, company_id: str) -> str | None:
        return self._plans.get((tenant_id, company_id))

    def remember_plan(self, tenant_id: str, company_id: str, command_digest: str) -> None:
        key = (tenant_id, company_id)
        existing = self._plans.get(key)
        if existing is not None and existing != command_digest:
            raise SetupAdminError("a different setup plan already exists")
        self._plans[key] = command_digest
