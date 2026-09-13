from __future__ import annotations

from .models import AuditRecord, PolicyEvaluation, RoleDefinition


class IdempotencyConflict(RuntimeError):
    pass


class InMemoryWorkforceRepository:
    """Offline reference repository. Every lookup requires tenant and company scope."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str, str, int], RoleDefinition] = {}
        self._current: dict[tuple[str, str, str], int] = {}
        self._evaluations: dict[tuple[str, str, str], PolicyEvaluation] = {}
        self._commands: dict[tuple[str, str, str], tuple[str, RoleDefinition]] = {}
        self._audit: list[AuditRecord] = []

    def add_definition(self, definition: RoleDefinition, *, make_current: bool = True) -> None:
        key = (definition.tenant_id, definition.company_id, definition.role_id, definition.version)
        if key in self._definitions:
            raise ValueError("role definition version already exists")
        self._definitions[key] = definition
        if make_current:
            self._current[key[:3]] = definition.version

    def replace_definition(self, definition: RoleDefinition) -> None:
        key = (definition.tenant_id, definition.company_id, definition.role_id, definition.version)
        if key not in self._definitions:
            raise LookupError("role definition not found in tenant/company scope")
        self._definitions[key] = definition

    def get_definition(
        self, tenant_id: str, company_id: str, role_id: str, version: int
    ) -> RoleDefinition | None:
        return self._definitions.get((tenant_id, company_id, role_id, version))

    def current_definition(self, tenant_id: str, company_id: str, role_id: str) -> RoleDefinition | None:
        version = self._current.get((tenant_id, company_id, role_id))
        return self.get_definition(tenant_id, company_id, role_id, version) if version else None

    def list_current(self, tenant_id: str, company_id: str) -> tuple[RoleDefinition, ...]:
        values = [
            self._definitions[(*scope, version)]
            for scope, version in self._current.items()
            if scope[:2] == (tenant_id, company_id)
        ]
        return tuple(sorted(values, key=lambda item: item.role_id))

    def save_evaluation(self, idempotency_key: str, evaluation: PolicyEvaluation) -> PolicyEvaluation:
        key = (evaluation.tenant_id, evaluation.company_id, idempotency_key)
        existing = self._evaluations.get(key)
        if existing:
            if existing.request_digest != evaluation.request_digest:
                raise IdempotencyConflict("idempotency key was reused for a different request")
            return existing
        self._evaluations[key] = evaluation
        return evaluation

    def get_evaluation(
        self, tenant_id: str, company_id: str, idempotency_key: str
    ) -> PolicyEvaluation | None:
        return self._evaluations.get((tenant_id, company_id, idempotency_key))

    def get_command(
        self, tenant_id: str, company_id: str, idempotency_key: str, command_digest: str
    ) -> RoleDefinition | None:
        existing = self._commands.get((tenant_id, company_id, idempotency_key))
        if not existing:
            return None
        if existing[0] != command_digest:
            raise IdempotencyConflict("idempotency key was reused for a different command")
        return existing[1]

    def save_command(
        self, tenant_id: str, company_id: str, idempotency_key: str,
        command_digest: str, result: RoleDefinition,
    ) -> RoleDefinition:
        key = (tenant_id, company_id, idempotency_key)
        existing = self._commands.get(key)
        if existing:
            if existing[0] != command_digest:
                raise IdempotencyConflict("idempotency key was reused for a different command")
            return existing[1]
        self._commands[key] = (command_digest, result)
        return result

    def append_audit(self, record: AuditRecord) -> None:
        self._audit.append(record)

    def list_audit(self, tenant_id: str, company_id: str) -> tuple[AuditRecord, ...]:
        return tuple(
            record for record in self._audit
            if record.tenant_id == tenant_id and record.company_id == company_id
        )

    def projection(self, tenant_id: str, company_id: str) -> dict[str, object]:
        roles = self.list_current(tenant_id, company_id)
        evaluations = [
            item for (tenant, company, _), item in self._evaluations.items()
            if (tenant, company) == (tenant_id, company_id)
        ]
        counts: dict[str, int] = {}
        for item in evaluations:
            counts[item.decision.value] = counts.get(item.decision.value, 0) + 1
        return {
            "tenant_id": tenant_id,
            "company_id": company_id,
            "roles": [role.to_projection() for role in roles],
            "decision_counts": counts,
            "evaluation_count": len(evaluations),
            "audit_count": len(self.list_audit(tenant_id, company_id)),
        }
