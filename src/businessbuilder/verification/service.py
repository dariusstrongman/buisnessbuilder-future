from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from .catalog import VerificationDefinition, VerificationDefinitionRegistry
from .models import DependencyRef, EvidenceRef, EvidenceType, VerificationRecord, VerificationState, utc_now
from .repository import VerificationRepository


class VerificationError(RuntimeError):
    pass


class IllegalTransitionError(VerificationError):
    pass


class MissingEvidenceError(VerificationError):
    pass


NEXT_STATE = {
    VerificationState.PROPOSED: VerificationState.EXECUTED,
    VerificationState.EXECUTED: VerificationState.TESTED,
    VerificationState.TESTED: VerificationState.VERIFIED,
    VerificationState.EXPIRED: VerificationState.EXECUTED,
}


class VerificationService:
    def __init__(self, repository: VerificationRepository, registry: VerificationDefinitionRegistry) -> None:
        self.repository = repository
        self.registry = registry

    def create(self, record: VerificationRecord) -> VerificationRecord:
        if record.state is not VerificationState.PROPOSED:
            raise IllegalTransitionError("new verification must start at proposed")
        self.registry.get(record.definition_id)
        self._check_evidence_scope(record)
        return self.repository.save(record)

    def get(self, tenant_id: str, company_id: str, verification_id: str) -> VerificationRecord:
        """Read through the service boundary without exposing repository internals."""
        return self.repository.get(tenant_id, company_id, verification_id)

    def list_for_company(self, tenant_id: str, company_id: str) -> tuple[VerificationRecord, ...]:
        return self.repository.list_for_company(tenant_id, company_id)

    def transition(
        self,
        tenant_id: str,
        company_id: str,
        verification_id: str,
        target_state: VerificationState,
        *,
        evidence: tuple[EvidenceRef, ...] = (),
        owner: str | None = None,
        scope: str | None = None,
        dependencies: tuple[DependencyRef, ...] | None = None,
        at: datetime | None = None,
    ) -> VerificationRecord:
        now = at or utc_now()
        record = self.repository.get(tenant_id, company_id, verification_id)
        definition = self.registry.get(record.definition_id)
        expected = NEXT_STATE.get(record.state)
        if target_state is not expected:
            raise IllegalTransitionError(f"illegal transition: {record.state.value} -> {target_state.value}")
        if record.failure_reason and target_state is VerificationState.VERIFIED:
            missing = self._missing_fresh_defined_tests(evidence, record, definition, now)
            if missing:
                raise MissingEvidenceError(
                    "new passing test evidence is required after a recorded failure for: "
                    + ", ".join(missing)
                )
        if record.stale_reason and target_state is VerificationState.TESTED:
            missing = self._missing_fresh_defined_tests(evidence, record, definition, now)
            if missing:
                raise MissingEvidenceError(
                    "new passing test evidence is required after invalidation for: " + ", ".join(missing)
                )
        candidate = record.with_update(
            state=target_state,
            evidence=self._merge_evidence(record.evidence, evidence),
            owner=owner if owner is not None else record.owner,
            scope=scope if scope is not None else record.scope,
            dependencies=dependencies if dependencies is not None else record.dependencies,
            failure_reason=None,
            stale_reason=None,
            updated_at=now,
        )
        self._check_evidence_scope(candidate)
        if target_state is VerificationState.TESTED:
            self._require_defined_test(candidate, definition, now)
        if target_state is VerificationState.VERIFIED:
            self._require_verified(candidate, definition, now)
            if candidate.expires_at is None and definition.default_ttl_seconds:
                candidate = replace(
                    candidate,
                    expires_at=now + timedelta(seconds=definition.default_ttl_seconds),
                )
        return self.repository.save(candidate)

    def record_failure(
        self,
        tenant_id: str,
        company_id: str,
        verification_id: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> VerificationRecord:
        if not reason.strip():
            raise ValueError("failure reason is required")
        now = at or utc_now()
        record = self.repository.get(tenant_id, company_id, verification_id)
        definition = self.registry.get(record.definition_id)
        fallback = self._highest_supported_state(record, definition, now, allow_verified=False)
        failed = record.with_update(
            state=fallback,
            failure_reason=reason,
            stale_reason=None,
            expires_at=None,
            provenance={**record.provenance, "failure_code": "VERIFICATION_FAILED"},
            updated_at=now,
        )
        return self.repository.save(failed)

    def expire_due(self, tenant_id: str, company_id: str, *, at: datetime | None = None) -> tuple[VerificationRecord, ...]:
        now = at or utc_now()
        changed: list[VerificationRecord] = []
        for record in self.repository.list_for_company(tenant_id, company_id):
            evidence_expired = any(not item.is_current(now) for item in record.evidence)
            record_expired = record.expires_at is not None and record.expires_at <= now
            if record.state is VerificationState.VERIFIED and (record_expired or evidence_expired):
                expired = record.with_update(
                    state=VerificationState.EXPIRED,
                    stale_reason="verification or supporting evidence expired",
                    updated_at=now,
                )
                changed.append(self.repository.save(expired))
        return tuple(changed)

    def invalidate_dependency(
        self,
        tenant_id: str,
        company_id: str,
        dependency_id: str,
        new_version: int,
        *,
        at: datetime | None = None,
    ) -> tuple[VerificationRecord, ...]:
        now = at or utc_now()
        changed: list[VerificationRecord] = []
        for record in self.repository.list_for_company(tenant_id, company_id):
            matching = [item for item in record.dependencies if item.dependency_id == dependency_id]
            if not matching or all(item.observed_version >= new_version for item in matching):
                continue
            definition = self.registry.get(record.definition_id)
            if record.state in {VerificationState.TESTED, VerificationState.VERIFIED, VerificationState.EXPIRED}:
                invalidated = record.with_update(
                    state=definition.invalidation_state,
                    expires_at=None,
                    stale_reason=f"dependency {dependency_id} changed to version {new_version}",
                    updated_at=now,
                )
                changed.append(self.repository.save(invalidated))
        return tuple(changed)

    @staticmethod
    def _merge_evidence(current: tuple[EvidenceRef, ...], additions: tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
        merged = {item.evidence_id: item for item in current}
        merged.update({item.evidence_id: item for item in additions})
        return tuple(merged.values())

    @staticmethod
    def _check_evidence_scope(record: VerificationRecord) -> None:
        if any(
            item.tenant_id != record.tenant_id or item.company_id != record.company_id
            for item in record.evidence
        ):
            raise VerificationError("cross-tenant or cross-company evidence is forbidden")

    @staticmethod
    def _require_defined_test(record: VerificationRecord, definition: VerificationDefinition, at: datetime) -> None:
        missing = VerificationService._missing_defined_tests(record.evidence, definition, at)
        if missing:
            raise MissingEvidenceError(
                "tested requires current passing results for every defined test; missing: "
                + ", ".join(missing)
            )

    @staticmethod
    def _missing_defined_tests(
        evidence: tuple[EvidenceRef, ...], definition: VerificationDefinition, at: datetime
    ) -> tuple[str, ...]:
        passed = {
            item.test_name
            for item in evidence
            if item.supports_defined_test(definition.defined_tests, at)
        }
        return tuple(sorted(definition.defined_tests - passed))

    @staticmethod
    def _missing_fresh_defined_tests(
        evidence: tuple[EvidenceRef, ...],
        record: VerificationRecord,
        definition: VerificationDefinition,
        at: datetime,
    ) -> tuple[str, ...]:
        existing_ids = {item.evidence_id for item in record.evidence}
        passed = {
            item.test_name
            for item in evidence
            if item.evidence_id not in existing_ids
            and record.updated_at <= item.captured_at <= at
            and item.supports_defined_test(definition.defined_tests, at)
        }
        return tuple(sorted(definition.defined_tests - passed))

    def _require_verified(self, record: VerificationRecord, definition: VerificationDefinition, at: datetime) -> None:
        if not record.owner or not record.scope.strip():
            raise MissingEvidenceError("verified requires owner and scope")
        if record.method not in definition.allowed_methods:
            raise MissingEvidenceError("verification method is not permitted by the definition")
        self._require_defined_test(record, definition, at)
        current_types = {item.evidence_type for item in record.evidence if item.is_current(at)}
        missing = definition.required_evidence_types - current_types
        if missing:
            values = ", ".join(sorted(item.value for item in missing))
            raise MissingEvidenceError(f"verified is missing current evidence types: {values}")
        # Screenshots can supplement, never satisfy, a definition on their own.
        if current_types == {EvidenceType.SCREENSHOT}:
            raise MissingEvidenceError("screenshot alone is not functional proof")

    def _highest_supported_state(
        self,
        record: VerificationRecord,
        definition: VerificationDefinition,
        at: datetime,
        *,
        allow_verified: bool,
    ) -> VerificationState:
        if allow_verified:
            try:
                self._require_verified(record, definition, at)
                return VerificationState.VERIFIED
            except MissingEvidenceError:
                pass
        try:
            self._require_defined_test(record, definition, at)
            return VerificationState.TESTED
        except MissingEvidenceError:
            return VerificationState.EXECUTED if record.state is not VerificationState.PROPOSED else VerificationState.PROPOSED
