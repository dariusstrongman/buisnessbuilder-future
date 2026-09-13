from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .model import BrainRecord, Company, RecordKind


SNAPSHOT_KINDS = frozenset({
    RecordKind.GOAL, RecordKind.STRATEGY, RecordKind.DECISION, RecordKind.OFFER,
    RecordKind.SERVICE, RecordKind.MARKET, RecordKind.POLICY, RecordKind.CAPACITY,
    RecordKind.ASSET, RecordKind.WORKFLOW, RecordKind.RISK, RecordKind.OBLIGATION,
})
SENSITIVE_PARTS = ("secret", "token", "password", "credential", "private_key", "pii")


def _safe(value: Any, key: str = "") -> Any:
    if any(part in key.lower() for part in SENSITIVE_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _safe(v, k) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def capability_snapshot(company: Company, records: Iterable[BrainRecord]) -> dict[str, Any]:
    selected = []
    for record in sorted(records, key=lambda item: (item.kind.value, item.record_id)):
        if record.kind not in SNAPSHOT_KINDS or record.invalidated_at:
            continue
        selected.append({
            "id": record.record_id,
            "kind": record.kind.value,
            "version": record.version,
            "knowledge_class": record.knowledge_class.value,
            "confidence": record.confidence,
            "owner_ref": record.owner_ref.to_dict(),
            "data": _safe(dict(record.data)),
            "provenance_digests": sorted(p.content_digest for p in record.provenance if p.content_digest),
        })
    body = {
        "schema_version": "company-brain.snapshot.v1",
        "tenant_id": company.scope.tenant_id,
        "company_id": company.scope.company_id,
        "company_version": company.version,
        "display_name": company.display_name,
        "archetype": company.archetype,
        "jurisdiction": _safe(dict(company.jurisdiction)),
        "lifecycle": company.lifecycle.value,
        "records": selected,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {**body, "snapshot_digest": "sha256:" + hashlib.sha256(canonical).hexdigest()}
