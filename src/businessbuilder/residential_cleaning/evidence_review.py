from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any, Callable, Protocol

from businessbuilder.access_broker import (
    ArtifactClassification,
    ArtifactRecord,
    ArtifactStatus,
    ReceiptStatus,
)
from businessbuilder.access_broker.ports import ArtifactStorePort
from businessbuilder.company_brain import Scope
from businessbuilder.identity import (
    AuthenticatedPrincipal,
    AuthorizationContext,
    AuthorizationPolicy,
    Permission,
    PrincipalContextAuthority,
    Role,
)
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.verification import EvidenceRef, EvidenceType


# The current customer HTTP server has a one-megabyte request limit. Base64 and
# JSON overhead make 512 KiB the largest honest v1 upload contract.
MAX_EVIDENCE_BYTES = 512 * 1024
SIGNED_ACCESS_SECONDS = 120
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,160}$")
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,159}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_UPLOADS = {
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
}


class SubmissionEvidenceType(StrEnum):
    FOUNDER_ATTESTATION = "founder_attestation"
    AUTHORITY_CONFIRMATION = "authority_confirmation"
    PROVIDER_RECEIPT = "provider_receipt"
    BUSINESS_BUILDER_TEST = "business_builder_test"
    SUPPORTING_SCREENSHOT = "supporting_screenshot"
    SUPPORTING_DOCUMENT = "supporting_document"
    OTHER_SUPPORTED_REFERENCE = "other_supported_reference"


class EvidenceSource(StrEnum):
    FILE_UPLOAD = "file_upload"
    AUTHORITY_REFERENCE = "authority_reference"
    PROVIDER_RECEIPT = "provider_receipt"
    STRUCTURED_REFERENCE = "structured_reference"
    SYSTEM = "system"


class ScanState(StrEnum):
    PENDING_SCAN = "pending_scan"
    CLEAN = "clean"
    REJECTED = "rejected"
    NOT_APPLICABLE = "not_applicable"


class ReviewDecision(StrEnum):
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"


@dataclass(frozen=True, slots=True)
class MalwareScanResult:
    state: ScanState
    scanner_id: str
    signature_version: str
    reason_code: str


class MalwareScannerPort(Protocol):
    def scan(self, content: bytes, *, filename: str, content_type: str) -> MalwareScanResult: ...


class PendingMalwareScanner:
    """Fail-closed boundary used when no production malware scanner is configured."""

    def scan(self, content: bytes, *, filename: str, content_type: str) -> MalwareScanResult:
        del content, filename, content_type
        return MalwareScanResult(
            ScanState.PENDING_SCAN,
            "unconfigured",
            "none",
            "production_scanner_not_configured",
        )


class DeterministicMalwareScanner:
    """Test-only scanner. It proves orchestration, not production malware detection."""

    def scan(self, content: bytes, *, filename: str, content_type: str) -> MalwareScanResult:
        del filename, content_type
        state = ScanState.REJECTED if b"DETERMINISTIC-MALWARE-MARKER" in content else ScanState.CLEAN
        return MalwareScanResult(
            state,
            "deterministic_test_scanner",
            "test-v1",
            "test_signature_match" if state is ScanState.REJECTED else "test_scan_clean",
        )


@dataclass(frozen=True, slots=True)
class EvidenceSubmission:
    submission_id: str
    tenant_id: str
    company_id: str
    action_id: str
    submitted_by: str
    evidence_type: SubmissionEvidenceType
    source: EvidenceSource
    immutable_reference: str
    content_sha256: str
    submitted_at: datetime
    scan_state: ScanState
    request_digest: str
    artifact_id: str | None = None
    provider_receipt_id: str | None = None
    safe_filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    authority_code: str | None = None
    reference_kind: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    supersedes_submission_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceReview:
    review_id: str
    tenant_id: str
    company_id: str
    action_id: str
    submission_ids: tuple[str, ...]
    decision: ReviewDecision
    reason_code: str
    operator_notes: str | None
    requested_additional_evidence: tuple[str, ...]
    reviewer_user_id: str
    reviewer_authority: str
    reviewed_at: datetime
    review_version: int
    request_digest: str
    supersedes_review_id: str | None = None


class EvidenceReviewConflict(RuntimeError):
    pass


class ResidentialCleaningEvidenceReviewService:
    """Pilot-only safe evidence intake and scoped human review boundary."""

    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        artifact_store: ArtifactStorePort,
        principal_authority: PrincipalContextAuthority,
        scanner: MalwareScannerPort | None,
        clock: Callable[[], datetime],
        id_factory: Callable[[str], str],
        maximum_bytes: int = MAX_EVIDENCE_BYTES,
    ) -> None:
        if maximum_bytes < 1 or maximum_bytes > MAX_EVIDENCE_BYTES:
            raise ValueError("residential-cleaning evidence size limit is invalid")
        self.repository = repository
        self.artifact_store = artifact_store
        self.principal_authority = principal_authority
        self.scanner = scanner or PendingMalwareScanner()
        self.clock = clock
        self.id_factory = id_factory
        self.maximum_bytes = maximum_bytes

    def submit_upload(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        evidence_type: str,
        filename: str,
        content_type: str,
        content: bytes,
        idempotency_key: str,
        supersedes_submission_id: str | None = None,
    ) -> EvidenceSubmission:
        verified = self._founder(principal)
        self._validate_action(action_id)
        selected = SubmissionEvidenceType(evidence_type)
        if selected not in {
            SubmissionEvidenceType.SUPPORTING_SCREENSHOT,
            SubmissionEvidenceType.SUPPORTING_DOCUMENT,
        }:
            raise PermissionError("uploaded files cannot claim authority, provider, or test provenance")
        if not isinstance(content, bytes) or not content or len(content) > self.maximum_bytes:
            raise ValueError("evidence upload is empty or exceeds the pilot size limit")
        safe_filename = self._safe_filename(filename)
        self._validate_content(content, safe_filename, content_type, selected)
        if supersedes_submission_id:
            self._submission(verified.tenant_id, verified.company_id or "", supersedes_submission_id, action_id)
        content_hash = sha256(content).hexdigest()
        request = {
            "operation": "upload",
            "action_id": action_id,
            "evidence_type": selected.value,
            "filename": safe_filename,
            "content_type": content_type,
            "content_sha256": content_hash,
            "supersedes_submission_id": supersedes_submission_id,
        }
        prior = self._idempotent(verified, idempotency_key, request)
        if prior:
            return prior
        submission_id = "cleaning_submission_" + self._suffix(
            verified.tenant_id, verified.company_id or "", idempotency_key
        )
        artifact_id = "artifact_" + submission_id
        object_key = (
            f"tenant/{verified.tenant_id}/company/{verified.company_id}/"
            f"residential-cleaning-evidence/{artifact_id}/{content_hash}"
        )
        artifact = ArtifactRecord(
            artifact_id=artifact_id,
            tenant_id=verified.tenant_id,
            company_id=verified.company_id or "",
            object_key=object_key,
            content_sha256=content_hash,
            content_type=content_type,
            size_bytes=len(content),
            classification=(
                ArtifactClassification.SCREENSHOT
                if selected is SubmissionEvidenceType.SUPPORTING_SCREENSHOT
                else ArtifactClassification.CUSTOMER_UPLOAD
            ),
            status=ArtifactStatus.QUARANTINED,
            provenance_ref=f"founder_upload:{submission_id}",
            created_at=self.clock(),
        )
        self.artifact_store.put(
            object_key,
            content,
            content_type=content_type,
            content_sha256=content_hash,
        )
        self.repository.save_broker_record(
            "artifact", artifact.artifact_id, artifact.tenant_id, artifact.company_id, artifact
        )
        try:
            scan = self.scanner.scan(content, filename=safe_filename, content_type=content_type)
        except Exception:
            scan = MalwareScanResult(
                ScanState.PENDING_SCAN,
                "scanner_unavailable",
                "unknown",
                "scanner_failed_closed",
            )
        if scan.state not in {ScanState.PENDING_SCAN, ScanState.CLEAN, ScanState.REJECTED}:
            raise ValueError("malware scanner returned an invalid state")
        if scan.state is ScanState.CLEAN:
            artifact = replace(artifact, status=ArtifactStatus.AVAILABLE)
        self.repository.save_broker_record(
            "artifact", artifact.artifact_id, artifact.tenant_id, artifact.company_id, artifact
        )
        now = self.clock()
        digest = self._digest(request)
        submission = EvidenceSubmission(
            submission_id,
            verified.tenant_id,
            verified.company_id or "",
            action_id,
            verified.user_id,
            selected,
            EvidenceSource.FILE_UPLOAD,
            f"artifact:{artifact_id}",
            content_hash,
            now,
            scan.state,
            digest,
            artifact_id=artifact_id,
            safe_filename=safe_filename,
            content_type=content_type,
            size_bytes=len(content),
            supersedes_submission_id=supersedes_submission_id,
        )
        self._save_submission(submission, idempotency_key)
        self._save_scan(submission, scan)
        self._audit(
            submission,
            verified.user_id,
            "cleaning.evidence.uploaded",
            "Evidence stored in private quarantine and classified",
            {"evidence_type": selected.value, "scan_state": scan.state.value, "size_bytes": len(content)},
        )
        return submission

    def submit_reference(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        evidence_type: str,
        source: str,
        reference: dict[str, Any],
        idempotency_key: str,
        supersedes_submission_id: str | None = None,
    ) -> EvidenceSubmission:
        verified = self._founder(principal)
        self._validate_action(action_id)
        selected = SubmissionEvidenceType(evidence_type)
        source_value = EvidenceSource(source)
        if source_value not in {
            EvidenceSource.AUTHORITY_REFERENCE,
            EvidenceSource.PROVIDER_RECEIPT,
            EvidenceSource.STRUCTURED_REFERENCE,
        }:
            raise ValueError("unsupported evidence reference source")
        if supersedes_submission_id:
            self._submission(verified.tenant_id, verified.company_id or "", supersedes_submission_id, action_id)
        normalized = self._validate_reference(
            verified.tenant_id,
            verified.company_id or "",
            action_id,
            selected,
            source_value,
            reference,
        )
        request = {
            "operation": "reference",
            "action_id": action_id,
            "evidence_type": selected.value,
            "source": source_value.value,
            "reference": normalized,
            "supersedes_submission_id": supersedes_submission_id,
        }
        prior = self._idempotent(verified, idempotency_key, request)
        if prior:
            return prior
        submission_id = "cleaning_submission_" + self._suffix(
            verified.tenant_id, verified.company_id or "", idempotency_key
        )
        now = self.clock()
        submission = EvidenceSubmission(
            submission_id=submission_id,
            tenant_id=verified.tenant_id,
            company_id=verified.company_id or "",
            action_id=action_id,
            submitted_by=verified.user_id,
            evidence_type=selected,
            source=source_value,
            immutable_reference=normalized["immutable_reference"],
            content_sha256=normalized["content_sha256"],
            submitted_at=now,
            scan_state=ScanState.NOT_APPLICABLE,
            request_digest=self._digest(request),
            artifact_id=normalized.get("artifact_id"),
            provider_receipt_id=normalized.get("provider_receipt_id"),
            authority_code=normalized.get("authority_code"),
            reference_kind=normalized.get("reference_kind"),
            expires_at=normalized.get("expires_at"),
            supersedes_submission_id=supersedes_submission_id,
        )
        self._save_submission(submission, idempotency_key)
        self._audit(
            submission,
            verified.user_id,
            "cleaning.evidence.reference_submitted",
            "Opaque scoped evidence reference captured",
            {"evidence_type": selected.value, "source": source_value.value},
        )
        return submission

    def review(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        submission_ids: list[str],
        decision: str,
        reason_code: str,
        idempotency_key: str,
        operator_notes: str | None = None,
        requested_additional_evidence: list[str] | None = None,
        supersedes_review_id: str | None = None,
    ) -> EvidenceReview:
        operator = self._operator(principal)
        self._validate_action(action_id)
        selected = ReviewDecision(decision)
        if selected is ReviewDecision.PENDING_REVIEW:
            raise ValueError("operator must make an explicit review decision")
        if not isinstance(submission_ids, list) or not 1 <= len(submission_ids) <= 10:
            raise ValueError("review requires a bounded evidence submission set")
        if len(set(submission_ids)) != len(submission_ids):
            raise ValueError("review evidence submissions must be unique")
        submissions = tuple(
            self._submission(operator.tenant_id, operator.company_id or "", item, action_id)
            for item in submission_ids
        )
        if any(item.submitted_by == operator.user_id for item in submissions):
            raise PermissionError("an evidence submitter cannot review their own evidence")
        for item in submissions:
            self._validate_integrity(item, require_clean=selected is ReviewDecision.ACCEPTED)
        reason = self._bounded_text(reason_code, 80, required=True)
        notes = self._bounded_text(operator_notes, 500, required=False)
        requested = tuple(
            self._bounded_text(item, 160, required=True)
            for item in (requested_additional_evidence or [])
        )
        if len(requested) > 10:
            raise ValueError("requested evidence list is too large")
        if selected is ReviewDecision.MORE_EVIDENCE_REQUIRED and not requested:
            raise ValueError("more-evidence decision must explain what is required")
        prior_review = None
        if supersedes_review_id:
            prior_review = self._review(
                operator.tenant_id, operator.company_id or "", supersedes_review_id, action_id
            )
        request = {
            "action_id": action_id,
            "submission_ids": sorted(submission_ids),
            "decision": selected.value,
            "reason_code": reason,
            "operator_notes": notes,
            "requested_additional_evidence": list(requested),
            "supersedes_review_id": supersedes_review_id,
        }
        review_id = "cleaning_review_" + self._suffix(
            operator.tenant_id, operator.company_id or "", idempotency_key
        )
        existing = self.repository.get_broker_record(
            "cleaning_evidence_review", operator.tenant_id, operator.company_id or "", review_id
        )
        digest = self._digest(request)
        if existing:
            if existing.request_digest != digest:
                raise EvidenceReviewConflict("review idempotency key was reused with different content")
            return existing
        self._require_idempotency(idempotency_key)
        active = self._active_reviews(operator.tenant_id, operator.company_id or "", action_id)
        overlapping = [
            item for item in active if set(item.submission_ids) & set(submission_ids)
        ]
        if overlapping and (
            prior_review is None or any(item.review_id != prior_review.review_id for item in overlapping)
        ):
            raise EvidenceReviewConflict("an active review must be explicitly superseded")
        if prior_review is not None and not (
            set(prior_review.submission_ids) & set(submission_ids)
        ):
            raise EvidenceReviewConflict("a superseding review must cover prior evidence")
        now = self.clock()
        review = EvidenceReview(
            review_id,
            operator.tenant_id,
            operator.company_id or "",
            action_id,
            tuple(sorted(submission_ids)),
            selected,
            reason,
            notes,
            requested,
            operator.user_id,
            "business_builder_operator",
            now,
            (prior_review.review_version + 1) if prior_review else 1,
            digest,
            supersedes_review_id,
        )
        self.repository.save_broker_record(
            "cleaning_evidence_review", review.review_id, review.tenant_id, review.company_id, review
        )
        for submission in submissions:
            self._audit(
                submission,
                operator.user_id,
                "cleaning.evidence.reviewed",
                "Authorized operator recorded an immutable evidence review",
                {
                    "review_id": review.review_id,
                    "decision": review.decision.value,
                    "reason_code": review.reason_code,
                    "review_version": review.review_version,
                },
            )
        return review

    def issue_download(
        self,
        principal: AuthenticatedPrincipal,
        *,
        action_id: str,
        submission_id: str,
    ) -> dict[str, Any]:
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        if verified.role not in {Role.OWNER, Role.ADMIN, Role.SUPPORT}:
            raise PermissionError("evidence access is restricted")
        self._require_artifact_access(verified)
        submission = self._submission(
            verified.tenant_id, verified.company_id or "", submission_id, action_id
        )
        if submission.artifact_id is None:
            raise PermissionError("this evidence has no downloadable artifact")
        self._validate_integrity(submission, require_clean=True)
        artifact = self.repository.get_broker_record(
            "artifact", verified.tenant_id, verified.company_id or "", submission.artifact_id
        )
        assert artifact is not None
        url = self.artifact_store.signed_download(
            artifact.object_key, expires_in_seconds=SIGNED_ACCESS_SECONDS
        )
        self._audit(
            submission,
            verified.user_id,
            "cleaning.evidence.access_issued",
            "Short-lived scoped evidence access issued",
            {"expires_in_seconds": SIGNED_ACCESS_SECONDS},
        )
        return {
            "submission_id": submission.submission_id,
            "download_url": url,
            "expires_at": self._iso(self.clock().timestamp() + SIGNED_ACCESS_SECONDS),
            "content_sha256": submission.content_sha256,
        }

    def list_submissions(self, tenant_id: str, company_id: str, action_id: str) -> tuple[EvidenceSubmission, ...]:
        return tuple(
            item for item in self.repository.list_broker_records(
                "cleaning_evidence_submission", tenant_id, company_id
            ) if item.action_id == action_id
        )

    def list_reviews(self, tenant_id: str, company_id: str, action_id: str) -> tuple[EvidenceReview, ...]:
        return tuple(
            item for item in self.repository.list_broker_records(
                "cleaning_evidence_review", tenant_id, company_id
            ) if item.action_id == action_id
        )

    def active_accepted_references(
        self, tenant_id: str, company_id: str, action_id: str
    ) -> tuple[list[dict[str, str]], tuple[str, ...]]:
        reviews = self._active_reviews(tenant_id, company_id, action_id)
        accepted = [item for item in reviews if item.decision is ReviewDecision.ACCEPTED]
        refs: dict[tuple[str, str], dict[str, str]] = {}
        review_ids: list[str] = []
        for review in accepted:
            review_ids.append(review.review_id)
            for submission_id in review.submission_ids:
                submission = self._submission(tenant_id, company_id, submission_id, action_id)
                self._validate_integrity(submission, require_clean=True)
                ref = self._runtime_reference(submission)
                if ref:
                    refs[(ref["kind"], ref["reference"])] = ref
        return list(refs.values()), tuple(sorted(review_ids))

    def accepted_evidence_types(
        self, tenant_id: str, company_id: str, action_id: str
    ) -> frozenset[EvidenceType]:
        values: set[EvidenceType] = set()
        for review in self._active_reviews(tenant_id, company_id, action_id):
            if review.decision is not ReviewDecision.ACCEPTED:
                continue
            for submission_id in review.submission_ids:
                submission = self._submission(tenant_id, company_id, submission_id, action_id)
                self._validate_integrity(submission, require_clean=True)
                mapped = {
                    SubmissionEvidenceType.AUTHORITY_CONFIRMATION: EvidenceType.AUTHORITY_CONFIRMATION,
                    SubmissionEvidenceType.PROVIDER_RECEIPT: EvidenceType.PROVIDER_RECEIPT,
                    SubmissionEvidenceType.SUPPORTING_SCREENSHOT: EvidenceType.SCREENSHOT,
                }.get(submission.evidence_type)
                if mapped:
                    values.add(mapped)
        return frozenset(values)

    def verification_evidence(
        self,
        scope: Scope,
        action_id: str,
        action: dict[str, Any],
        *,
        at: datetime,
    ) -> tuple[EvidenceRef, ...]:
        active = self._active_reviews(scope.tenant_id, scope.company_id, action_id)
        if not active or any(item.decision is not ReviewDecision.ACCEPTED for item in active):
            raise PermissionError("an active accepted operator review is required")
        accepted_refs, accepted_review_ids = self.active_accepted_references(
            scope.tenant_id, scope.company_id, action_id
        )
        accepted_pairs = {(item["kind"], item["reference"]) for item in accepted_refs}
        captured_pairs = {
            (item["source_class"], item["reference"])
            for item in action.get("evidence", [])
            if item["source_class"] in {"artifact", "provider_receipt"}
        }
        from .founder_actions import DEFINITIONS_BY_ID
        required_external = set(DEFINITIONS_BY_ID[action_id].required_evidence) - {
            EvidenceType.FOUNDER_ATTESTATION,
            EvidenceType.TEST_RESULT,
        }
        if (
            (required_external and not captured_pairs)
            or not captured_pairs.issubset(accepted_pairs)
        ):
            raise PermissionError("captured evidence is not covered by current accepted review")
        evidence: list[EvidenceRef] = []
        for review in active:
            digest = self._digest(review)
            evidence.append(
                EvidenceRef(
                    evidence_id="evidence_review_" + digest[:24],
                    evidence_type=EvidenceType.HUMAN_REVIEW,
                    artifact_ref=f"operator-review:{review.review_id}:{digest}",
                    tenant_id=scope.tenant_id,
                    company_id=scope.company_id,
                    captured_at=review.reviewed_at,
                    issuer=review.reviewer_user_id,
                    provenance={
                        "reviewer_authority": review.reviewer_authority,
                        "review_version": review.review_version,
                        "accepted_review_ids": list(accepted_review_ids),
                    },
                )
            )
        return tuple(evidence)

    def public_action_state(self, tenant_id: str, company_id: str, action_id: str) -> dict[str, Any]:
        submissions = self.list_submissions(tenant_id, company_id, action_id)
        reviews = self.list_reviews(tenant_id, company_id, action_id)
        active_ids = {item.review_id for item in self._active_reviews(tenant_id, company_id, action_id)}
        review_by_submission: dict[str, EvidenceReview] = {}
        for review in reviews:
            for submission_id in review.submission_ids:
                prior = review_by_submission.get(submission_id)
                if prior is None or (review.reviewed_at, review.review_version, review.review_id) > (
                    prior.reviewed_at, prior.review_version, prior.review_id
                ):
                    review_by_submission[submission_id] = review
        return {
            "submissions": [
                self.public_submission(item, review_by_submission.get(item.submission_id))
                for item in submissions
            ],
            "reviews": [self.public_review(item, item.review_id in active_ids) for item in reviews],
            "review_state": self._summary_state(
                tuple(item for item in reviews if item.review_id in active_ids)
            ),
        }

    @staticmethod
    def public_submission(item: EvidenceSubmission, review: EvidenceReview | None = None) -> dict[str, Any]:
        return {
            "submission_id": item.submission_id,
            "action_id": item.action_id,
            "evidence_type": item.evidence_type.value,
            "source": item.source.value,
            "safe_filename": item.safe_filename,
            "content_type": item.content_type,
            "size_bytes": item.size_bytes,
            "content_sha256": item.content_sha256,
            "scan_state": item.scan_state.value,
            "submitted_at": ResidentialCleaningEvidenceReviewService._iso(item.submitted_at),
            "expires_at": ResidentialCleaningEvidenceReviewService._iso(item.expires_at),
            "revoked": item.revoked_at is not None,
            "supersedes_submission_id": item.supersedes_submission_id,
            "review_state": review.decision.value if review else ReviewDecision.PENDING_REVIEW.value,
            "review_reason": review.reason_code if review else None,
        }

    @staticmethod
    def public_review(item: EvidenceReview, active: bool) -> dict[str, Any]:
        return {
            "review_id": item.review_id,
            "submission_ids": list(item.submission_ids),
            "decision": item.decision.value,
            "reason_code": item.reason_code,
            "requested_additional_evidence": list(item.requested_additional_evidence),
            "reviewed_at": ResidentialCleaningEvidenceReviewService._iso(item.reviewed_at),
            "review_version": item.review_version,
            "supersedes_review_id": item.supersedes_review_id,
            "active": active,
        }

    def _validate_reference(self, tenant_id, company_id, action_id, evidence_type, source, reference):
        if not isinstance(reference, dict):
            raise ValueError("evidence reference must be an object")
        from .founder_actions import DEFINITIONS_BY_ID, EVIDENCE_CAPABILITY

        definition = DEFINITIONS_BY_ID[action_id]
        if source is EvidenceSource.AUTHORITY_REFERENCE:
            if evidence_type is not SubmissionEvidenceType.AUTHORITY_CONFIRMATION or set(reference) != {"artifact_id"}:
                raise ValueError("authority evidence requires one opaque artifact reference")
            artifact_id = reference["artifact_id"]
            self._safe_ref(artifact_id)
            artifact = self.repository.get_broker_record("artifact", tenant_id, company_id, artifact_id)
            if (
                artifact is None
                or artifact.status is not ArtifactStatus.AVAILABLE
                or artifact.classification is not ArtifactClassification.VERIFICATION_EVIDENCE
                or artifact.provenance_ref != f"verified_authority:{definition.authority_code}"
            ):
                raise PermissionError("authority reference is not authentic for this action")
            return {
                "immutable_reference": f"artifact:{artifact_id}",
                "content_sha256": artifact.content_sha256,
                "artifact_id": artifact_id,
                "authority_code": definition.authority_code,
                "expires_at": artifact.expires_at,
            }
        if source is EvidenceSource.PROVIDER_RECEIPT:
            if evidence_type is not SubmissionEvidenceType.PROVIDER_RECEIPT or set(reference) != {"provider_receipt_id"}:
                raise ValueError("provider evidence requires one opaque receipt reference")
            receipt_id = reference["provider_receipt_id"]
            self._safe_ref(receipt_id)
            receipt = next(
                (item for item in self.repository.list_provider_receipts(tenant_id, company_id)
                 if item.receipt_id == receipt_id),
                None,
            )
            if (
                receipt is None
                or receipt.status is not ReceiptStatus.SUCCEEDED
                or receipt.capability != EVIDENCE_CAPABILITY
                or receipt.operation != definition.provider_operation
            ):
                raise PermissionError("provider receipt is not authentic for this action")
            digest = sha256(
                f"{receipt.receipt_id}\0{receipt.provider_request_id}\0{receipt.status.value}".encode()
            ).hexdigest()
            return {
                "immutable_reference": f"provider_receipt:{receipt_id}",
                "content_sha256": digest,
                "provider_receipt_id": receipt_id,
            }
        if source is EvidenceSource.STRUCTURED_REFERENCE:
            if evidence_type is not SubmissionEvidenceType.OTHER_SUPPORTED_REFERENCE:
                raise PermissionError("unverified structured references cannot claim stronger provenance")
            allowed = {"reference_id", "reference_kind", "issued_at", "content_sha256"}
            if set(reference) != allowed:
                raise ValueError("structured reference fields are invalid")
            self._safe_ref(reference["reference_id"])
            kind = self._bounded_text(reference["reference_kind"], 80, required=True)
            self._parse_time(reference["issued_at"])
            if not isinstance(reference["content_sha256"], str) or not _SHA256.fullmatch(reference["content_sha256"]):
                raise ValueError("structured reference SHA-256 is invalid")
            digest = sha256(json.dumps(reference, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return {
                "immutable_reference": f"structured:{reference['reference_id']}:{digest[:24]}",
                "content_sha256": reference["content_sha256"],
                "reference_kind": kind,
            }
        raise ValueError("unsupported reference source")

    def _validate_integrity(self, submission: EvidenceSubmission, *, require_clean: bool) -> None:
        now = self.clock()
        if submission.revoked_at is not None or (submission.expires_at and submission.expires_at <= now):
            raise PermissionError("evidence submission is revoked or expired")
        if require_clean and submission.scan_state not in {ScanState.CLEAN, ScanState.NOT_APPLICABLE}:
            raise PermissionError("evidence has not passed the required safety check")
        if submission.artifact_id:
            artifact = self.repository.get_broker_record(
                "artifact", submission.tenant_id, submission.company_id, submission.artifact_id
            )
            if (
                artifact is None
                or artifact.content_sha256 != submission.content_sha256
                or (require_clean and artifact.status is not ArtifactStatus.AVAILABLE)
                or artifact.expires_at != submission.expires_at
            ):
                raise PermissionError("evidence artifact metadata changed or is unavailable")
            with self.artifact_store.read(artifact.object_key, maximum_bytes=self.maximum_bytes) as material:
                actual = material.use(lambda value: sha256(value).hexdigest())
            if actual != submission.content_sha256:
                raise PermissionError("evidence artifact content failed integrity validation")
        if submission.provider_receipt_id:
            from .founder_actions import DEFINITIONS_BY_ID, EVIDENCE_CAPABILITY
            definition = DEFINITIONS_BY_ID[submission.action_id]
            receipt = next(
                (item for item in self.repository.list_provider_receipts(
                    submission.tenant_id, submission.company_id
                ) if item.receipt_id == submission.provider_receipt_id),
                None,
            )
            expected_digest = (
                sha256(
                    f"{receipt.receipt_id}\0{receipt.provider_request_id}\0{receipt.status.value}".encode()
                ).hexdigest()
                if receipt is not None else None
            )
            if (
                receipt is None
                or receipt.status is not ReceiptStatus.SUCCEEDED
                or receipt.capability != EVIDENCE_CAPABILITY
                or receipt.operation != definition.provider_operation
                or expected_digest != submission.content_sha256
            ):
                raise PermissionError("provider evidence is no longer valid")
        if submission.evidence_type is SubmissionEvidenceType.AUTHORITY_CONFIRMATION:
            from .founder_actions import DEFINITIONS_BY_ID
            definition = DEFINITIONS_BY_ID[submission.action_id]
            artifact = self.repository.get_broker_record(
                "artifact", submission.tenant_id, submission.company_id,
                submission.artifact_id or "missing",
            )
            if (
                artifact is None
                or artifact.classification is not ArtifactClassification.VERIFICATION_EVIDENCE
                or artifact.provenance_ref != f"verified_authority:{definition.authority_code}"
            ):
                raise PermissionError("authority evidence provenance is no longer valid")

    def _runtime_reference(self, submission: EvidenceSubmission) -> dict[str, str] | None:
        if submission.evidence_type in {
            SubmissionEvidenceType.AUTHORITY_CONFIRMATION,
            SubmissionEvidenceType.SUPPORTING_SCREENSHOT,
        } and submission.artifact_id:
            return {"kind": "artifact", "reference": submission.artifact_id}
        if submission.evidence_type is SubmissionEvidenceType.PROVIDER_RECEIPT and submission.provider_receipt_id:
            return {"kind": "provider_receipt", "reference": submission.provider_receipt_id}
        return None

    def _founder(self, principal: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        if verified.role is not Role.OWNER or verified.support_impersonation_session_id:
            raise PermissionError("only the authenticated founder may submit pilot evidence")
        return verified

    def _operator(self, principal: AuthenticatedPrincipal) -> AuthenticatedPrincipal:
        verified = self.principal_authority.verify(
            principal, tenant_id=principal.tenant_id, company_id=principal.company_id or ""
        )
        if verified.role is not Role.SUPPORT or not verified.support_impersonation_session_id:
            raise PermissionError("an audited scoped Business Builder operator session is required")
        self._require_artifact_access(verified)
        return verified

    def _require_artifact_access(self, principal: AuthenticatedPrincipal) -> None:
        AuthorizationPolicy(self.principal_authority.repository).require(
            AuthorizationContext(
                principal.user_id,
                principal.tenant_id,
                principal.company_id,
                principal.support_impersonation_session_id,
            ),
            Permission.ACCESS_ARTIFACTS,
            at=self.clock(),
        )

    def _submission(self, tenant_id: str, company_id: str, submission_id: str, action_id: str) -> EvidenceSubmission:
        item = self.repository.get_broker_record(
            "cleaning_evidence_submission", tenant_id, company_id, submission_id
        )
        if item is None or item.action_id != action_id:
            raise LookupError("evidence submission not found in action scope")
        return item

    def _review(self, tenant_id: str, company_id: str, review_id: str, action_id: str) -> EvidenceReview:
        item = self.repository.get_broker_record(
            "cleaning_evidence_review", tenant_id, company_id, review_id
        )
        if item is None or item.action_id != action_id:
            raise LookupError("evidence review not found in action scope")
        return item

    def _active_reviews(self, tenant_id: str, company_id: str, action_id: str) -> tuple[EvidenceReview, ...]:
        reviews = self.list_reviews(tenant_id, company_id, action_id)
        superseded = {item.supersedes_review_id for item in reviews if item.supersedes_review_id}
        submissions = self.list_submissions(tenant_id, company_id, action_id)
        superseded_submissions = {
            item.supersedes_submission_id for item in submissions if item.supersedes_submission_id
        }
        active_submissions = {
            item.submission_id for item in submissions if item.submission_id not in superseded_submissions
        }
        return tuple(
            item for item in reviews
            if item.review_id not in superseded
            and any(submission_id in active_submissions for submission_id in item.submission_ids)
        )

    def _idempotent(self, principal, key: str, request: dict[str, Any]) -> EvidenceSubmission | None:
        self._require_idempotency(key)
        submission_id = "cleaning_submission_" + self._suffix(
            principal.tenant_id, principal.company_id or "", key
        )
        current = self.repository.get_broker_record(
            "cleaning_evidence_submission", principal.tenant_id, principal.company_id or "", submission_id
        )
        if current and current.request_digest != self._digest(request):
            raise EvidenceReviewConflict("evidence idempotency key was reused with different content")
        return current

    def _save_submission(self, submission: EvidenceSubmission, idempotency_key: str) -> None:
        self._require_idempotency(idempotency_key)
        self.repository.save_broker_record(
            "cleaning_evidence_submission",
            submission.submission_id,
            submission.tenant_id,
            submission.company_id,
            submission,
        )

    def _save_scan(self, submission: EvidenceSubmission, scan: MalwareScanResult) -> None:
        record = {
            "scan_id": "scan_" + submission.submission_id,
            "submission_id": submission.submission_id,
            "tenant_id": submission.tenant_id,
            "company_id": submission.company_id,
            "state": scan.state.value,
            "scanner_id": scan.scanner_id,
            "signature_version": scan.signature_version,
            "reason_code": scan.reason_code,
            "scanned_at": self.clock(),
        }
        self.repository.save_broker_record(
            "cleaning_evidence_scan",
            record["scan_id"],
            submission.tenant_id,
            submission.company_id,
            record,
        )

    def _audit(self, submission, actor_id, action, reason, metadata):
        self.repository.append_audit({
            "audit_event_id": self.id_factory("runtime_audit"),
            "tenant_id": submission.tenant_id,
            "company_id": submission.company_id,
            "actor_type": "operator" if action.endswith("reviewed") else "founder_or_authorized_reader",
            "actor_id": actor_id,
            "action": action,
            "target_type": "evidence_submission",
            "target_id": submission.submission_id,
            "correlation_id": "correlation_" + self._suffix(submission.submission_id, action),
            "occurred_at": self.clock(),
            "reason": reason,
            "before": None,
            "after": metadata,
        })

    @staticmethod
    def _validate_action(action_id: str) -> None:
        from .founder_actions import DEFINITIONS_BY_ID
        if action_id not in DEFINITIONS_BY_ID:
            raise PermissionError("action is outside the residential-cleaning pilot")

    @staticmethod
    def _validate_content(content: bytes, filename: str, content_type: str, evidence_type: SubmissionEvidenceType) -> None:
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if content_type not in _ALLOWED_UPLOADS or suffix not in _ALLOWED_UPLOADS[content_type]:
            raise ValueError("evidence file type is not supported")
        signatures = {
            "application/pdf": content.startswith(b"%PDF-"),
            "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        }
        if not signatures[content_type]:
            raise ValueError("evidence content does not match its declared MIME type")
        if evidence_type is SubmissionEvidenceType.SUPPORTING_SCREENSHOT and content_type not in {"image/png", "image/jpeg"}:
            raise ValueError("supporting screenshots must be PNG or JPEG")

    @staticmethod
    def _safe_filename(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 120 or any(c in value for c in "\r\n\0/\\"):
            raise ValueError("evidence filename is invalid")
        cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", value).strip(" .")
        if not cleaned or cleaned.startswith(".") or ".." in cleaned:
            raise ValueError("evidence filename is invalid")
        return cleaned

    @staticmethod
    def _bounded_text(value: object, maximum: int, *, required: bool) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise ValueError("review text is invalid")
        cleaned = value.strip()
        if (required and not cleaned) or len(cleaned) > maximum or any(c in cleaned for c in "\0"):
            raise ValueError("review text is invalid")
        return cleaned or None

    @staticmethod
    def _safe_ref(value: object) -> str:
        if not isinstance(value, str) or not _SAFE_REFERENCE.fullmatch(value):
            raise ValueError("opaque evidence reference is invalid")
        return value

    @staticmethod
    def _require_idempotency(value: str) -> None:
        if not isinstance(value, str) or not _IDEMPOTENCY.fullmatch(value):
            raise ValueError("a canonical idempotency key is required")

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if not isinstance(value, str):
            raise ValueError("reference timestamp is invalid")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("reference timestamp must include timezone")
        return parsed

    @staticmethod
    def _digest(value: object) -> str:
        def default(item):
            if isinstance(item, datetime):
                return item.astimezone(timezone.utc).isoformat()
            if isinstance(item, StrEnum):
                return item.value
            if hasattr(item, "__dataclass_fields__"):
                return {name: getattr(item, name) for name in item.__dataclass_fields__}
            raise TypeError(type(item).__name__)
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=default).encode()).hexdigest()

    @staticmethod
    def _suffix(*values: str) -> str:
        return sha256("\0".join(values).encode()).hexdigest()[:24]

    @staticmethod
    def _iso(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _summary_state(reviews: tuple[EvidenceReview, ...]) -> str:
        values = {item.decision for item in reviews}
        if ReviewDecision.MORE_EVIDENCE_REQUIRED in values:
            return ReviewDecision.MORE_EVIDENCE_REQUIRED.value
        if ReviewDecision.REJECTED in values:
            return ReviewDecision.REJECTED.value
        if ReviewDecision.ACCEPTED in values:
            return ReviewDecision.ACCEPTED.value
        return ReviewDecision.PENDING_REVIEW.value


def decode_evidence_record(kind: str, data: dict[str, Any]):
    if kind == "cleaning_evidence_submission":
        data["evidence_type"] = SubmissionEvidenceType(data["evidence_type"])
        data["source"] = EvidenceSource(data["source"])
        data["scan_state"] = ScanState(data["scan_state"])
        return EvidenceSubmission(**data)
    if kind == "cleaning_evidence_review":
        data["submission_ids"] = tuple(data["submission_ids"])
        data["decision"] = ReviewDecision(data["decision"])
        data["requested_additional_evidence"] = tuple(data["requested_additional_evidence"])
        return EvidenceReview(**data)
    if kind == "cleaning_evidence_scan":
        return data
    raise ValueError("unsupported residential-cleaning evidence record kind")
