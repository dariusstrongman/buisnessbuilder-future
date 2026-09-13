from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any, Mapping, Sequence

from .errors import ValidationError
from .model import (
    BrainRecord, Company, Dependency, EntityRef, InvalidationNotice, KnowledgeClass,
    LifecycleState, MATERIAL_INVALIDATION_KINDS, Provenance, RecordKind, Scope,
    require_transition, utc_now,
)
from .repository import CompanyBrainRepository
from .snapshot import capability_snapshot


class CompanyBrainService:
    def __init__(self, repository: CompanyBrainRepository) -> None:
        self.repository = repository

    def create_company(self, company: Company) -> Company:
        return self.repository.create_company(company)

    def get_company(self, scope: Scope) -> Company:
        return self.repository.get_company(scope)

    def transition_company(self, scope: Scope, target: LifecycleState, *, expected_version: int) -> Company:
        current = self.repository.get_company(scope)
        if current.version != expected_version:
            from .errors import ConflictError
            raise ConflictError("company version conflict")
        require_transition(current.lifecycle, target)
        readiness = "fully_set" if target == LifecycleState.FULLY_SET else "ready" if target in {LifecycleState.READY, LifecycleState.OPERATING} else current.readiness
        updated = replace(current, lifecycle=target, readiness=readiness, version=current.version + 1, updated_at=utc_now())
        return self.repository.save_company(updated, expected_version=current.version)

    def update_company_profile(
        self, scope: Scope, *, expected_version: int, display_name: str | None = None,
        legal_name: str | None = None, archetype: str | None = None,
        jurisdiction: Mapping[str, Any] | None = None,
        owner_refs: Sequence[EntityRef] | None = None,
    ) -> tuple[Company, list[InvalidationNotice]]:
        """Version a material company-profile change and emit dependency notices."""
        current = self.repository.get_company(scope)
        if current.version != expected_version:
            from .errors import ConflictError
            raise ConflictError("company version conflict")
        changed_sources: list[tuple[str, str]] = []
        if jurisdiction is not None and dict(jurisdiction) != dict(current.jurisdiction):
            changed_sources.append(("company.jurisdiction", "jurisdiction changed"))
        if owner_refs is not None and tuple(owner_refs) != current.owner_refs:
            changed_sources.append(("company.owners", "company owner changed"))
        updated = replace(
            current,
            display_name=current.display_name if display_name is None else display_name,
            legal_name=current.legal_name if legal_name is None else legal_name,
            archetype=current.archetype if archetype is None else archetype,
            jurisdiction=current.jurisdiction if jurisdiction is None else dict(jurisdiction),
            owner_refs=current.owner_refs if owner_refs is None else tuple(owner_refs),
            version=current.version + 1,
            updated_at=utc_now(),
        )
        saved = self.repository.save_company(updated, expected_version=current.version)
        notices: list[InvalidationNotice] = []
        for source_id, reason in changed_sources:
            notices.extend(self._emit_invalidations(scope, source_id, saved.version, reason))
        return saved, notices

    def update_approved_state(
        self, scope: Scope, *, record_id: str, kind: RecordKind, data: Mapping[str, Any],
        knowledge_class: KnowledgeClass, provenance: Sequence[Provenance], confidence: float | None,
        owner_ref: EntityRef, lifecycle: str = "active", expected_version: int | None = None,
        invalidation_reason: str | None = None,
    ) -> tuple[BrainRecord, list[InvalidationNotice]]:
        now = utc_now()
        version = 1 if expected_version is None else expected_version + 1
        record = BrainRecord(
            scope=scope, record_id=record_id, kind=kind, data=dict(data), knowledge_class=knowledge_class,
            owner_ref=owner_ref, provenance=tuple(provenance), confidence=confidence, lifecycle=lifecycle,
            version=version, created_at=now, updated_at=now,
            supersedes_version=expected_version,
        )
        saved = self.repository.append_record(record, expected_version=expected_version)
        notices: list[InvalidationNotice] = []
        if expected_version is not None and kind in MATERIAL_INVALIDATION_KINDS:
            reason = invalidation_reason or f"material {kind.value} changed"
            notices.extend(self._emit_invalidations(scope, record_id, saved.version, reason, occurred_at=now))
        return saved, notices

    def record_decision(self, scope: Scope, *, decision_id: str, data: Mapping[str, Any], provenance: Sequence[Provenance], owner_ref: EntityRef, expected_version: int | None = None) -> BrainRecord:
        return self.update_approved_state(scope, record_id=decision_id, kind=RecordKind.DECISION, data=data,
                                          knowledge_class=KnowledgeClass.FOUNDER_DECISION, provenance=provenance,
                                          confidence=None, owner_ref=owner_ref, expected_version=expected_version)[0]

    def record_fact(self, scope: Scope, *, record_id: str, kind: RecordKind, data: Mapping[str, Any], provenance: Sequence[Provenance], owner_ref: EntityRef, expected_version: int | None = None) -> BrainRecord:
        return self.update_approved_state(scope, record_id=record_id, kind=kind, data=data,
                                          knowledge_class=KnowledgeClass.FACT, provenance=provenance,
                                          confidence=None, owner_ref=owner_ref, expected_version=expected_version)[0]

    def record_estimate(self, scope: Scope, *, record_id: str, kind: RecordKind, data: Mapping[str, Any], confidence: float, provenance: Sequence[Provenance], owner_ref: EntityRef, expected_version: int | None = None) -> BrainRecord:
        if not 0 <= confidence <= 1:
            raise ValidationError("estimate confidence must be between 0 and 1")
        return self.update_approved_state(scope, record_id=record_id, kind=kind, data=data,
                                          knowledge_class=KnowledgeClass.ESTIMATE, provenance=provenance,
                                          confidence=confidence, owner_ref=owner_ref, expected_version=expected_version)[0]

    def record_inference(self, scope: Scope, *, record_id: str, kind: RecordKind, data: Mapping[str, Any], confidence: float, provenance: Sequence[Provenance], owner_ref: EntityRef, expected_version: int | None = None) -> BrainRecord:
        if not 0 <= confidence <= 1:
            raise ValidationError("inference confidence must be between 0 and 1")
        return self.update_approved_state(scope, record_id=record_id, kind=kind, data=data,
                                          knowledge_class=KnowledgeClass.INFERENCE, provenance=provenance,
                                          confidence=confidence, owner_ref=owner_ref, expected_version=expected_version)[0]

    def attach_artifact(self, scope: Scope, *, record_id: str, artifact_ref: EntityRef, provenance: Sequence[Provenance], owner_ref: EntityRef) -> BrainRecord:
        return self.record_fact(scope, record_id=record_id, kind=RecordKind.ARTIFACT_REF,
                                data={"artifact_ref": artifact_ref.to_dict()}, provenance=provenance, owner_ref=owner_ref)

    def attach_evidence(self, scope: Scope, *, record_id: str, evidence_ref: EntityRef, provenance: Sequence[Provenance], owner_ref: EntityRef, external_verified: bool = False) -> BrainRecord:
        classification = KnowledgeClass.EXTERNAL_VERIFICATION if external_verified else KnowledgeClass.FACT
        return self.update_approved_state(scope, record_id=record_id, kind=RecordKind.EVIDENCE_REF,
                                          data={"evidence_ref": evidence_ref.to_dict()}, knowledge_class=classification,
                                          provenance=provenance, confidence=None, owner_ref=owner_ref)[0]

    def append_founder_action(self, scope: Scope, *, action_id: str, data: Mapping[str, Any], provenance: Sequence[Provenance], owner_ref: EntityRef) -> BrainRecord:
        return self.record_fact(scope, record_id=action_id, kind=RecordKind.FOUNDER_ACTION, data=data, provenance=provenance, owner_ref=owner_ref)

    def append_approval(self, scope: Scope, *, approval_id: str, approval_ref: EntityRef, provenance: Sequence[Provenance], owner_ref: EntityRef) -> BrainRecord:
        return self.record_fact(scope, record_id=approval_id, kind=RecordKind.APPROVAL_REF, data={"approval_ref": approval_ref.to_dict()}, provenance=provenance, owner_ref=owner_ref)

    def append_verification(self, scope: Scope, *, verification_id: str, verification_ref: EntityRef, provenance: Sequence[Provenance], owner_ref: EntityRef, external: bool = False) -> BrainRecord:
        return self.update_approved_state(scope, record_id=verification_id, kind=RecordKind.VERIFICATION_REF,
                                          data={"verification_ref": verification_ref.to_dict()},
                                          knowledge_class=KnowledgeClass.EXTERNAL_VERIFICATION if external else KnowledgeClass.FACT,
                                          provenance=provenance, confidence=None, owner_ref=owner_ref)[0]

    def append_audit_event(self, scope: Scope, *, event_id: str, data: Mapping[str, Any], provenance: Sequence[Provenance], owner_ref: EntityRef) -> BrainRecord:
        return self.record_fact(scope, record_id=event_id, kind=RecordKind.AUDIT_EVENT,
                                data=data, provenance=provenance, owner_ref=owner_ref)

    def add_dependency(self, scope: Scope, *, source_record_id: str, dependent_ref: EntityRef, trigger: str) -> None:
        self.repository.add_dependency(Dependency(scope, source_record_id, dependent_ref, trigger))

    def query_current_state(self, scope: Scope, *, kinds: Sequence[RecordKind] | None = None) -> list[BrainRecord]:
        self.repository.get_company(scope)
        return self.repository.list_current(scope, kinds=kinds)

    def query_history(self, scope: Scope, *, record_id: str | None = None) -> list[BrainRecord]:
        self.repository.get_company(scope)
        return self.repository.history(scope, record_id)

    def query_company_history(self, scope: Scope) -> list[Company]:
        return self.repository.company_history(scope)

    def compact_snapshot(self, scope: Scope) -> dict[str, Any]:
        return capability_snapshot(self.repository.get_company(scope), self.repository.list_current(scope))

    def _emit_invalidations(self, scope: Scope, source_id: str, source_version: int, reason: str, *, occurred_at: str | None = None) -> list[InvalidationNotice]:
        notices: list[InvalidationNotice] = []
        for dependency in self.repository.dependencies_for(scope, source_id):
            notice = InvalidationNotice(
                scope=scope, notice_id="inv_" + uuid.uuid4().hex, source_record_id=source_id,
                source_version=source_version, dependent_ref=dependency.dependent_ref,
                trigger=dependency.trigger, reason=reason, occurred_at=occurred_at or utc_now(),
            )
            self.repository.append_invalidation(notice)
            notices.append(notice)
        return notices
