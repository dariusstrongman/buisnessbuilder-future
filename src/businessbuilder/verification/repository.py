from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from .models import VerificationRecord


class VerificationRepository(Protocol):
    def save(self, record: VerificationRecord) -> VerificationRecord: ...
    def get(self, tenant_id: str, company_id: str, verification_id: str) -> VerificationRecord: ...
    def list_for_company(self, tenant_id: str, company_id: str) -> tuple[VerificationRecord, ...]: ...


class InMemoryVerificationRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], VerificationRecord] = {}

    def save(self, record: VerificationRecord) -> VerificationRecord:
        key = (record.tenant_id, record.company_id, record.verification_id)
        existing = self._records.get(key)
        if existing and record.version <= existing.version:
            raise ValueError("verification version must increase")
        self._records[key] = record
        return record

    def get(self, tenant_id: str, company_id: str, verification_id: str) -> VerificationRecord:
        try:
            return self._records[(tenant_id, company_id, verification_id)]
        except KeyError as exc:
            raise KeyError("verification not found in tenant/company scope") from exc

    def list_for_company(self, tenant_id: str, company_id: str) -> tuple[VerificationRecord, ...]:
        return tuple(
            record
            for (tenant, company, _), record in self._records.items()
            if tenant == tenant_id and company == company_id
        )


class JsonVerificationRepository(InMemoryVerificationRepository):
    """Small dev-safe JSON store; production storage remains behind the same port."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            values = json.loads(self.path.read_text(encoding="utf-8"))
            for value in values:
                record = VerificationRecord.from_dict(value)
                self._records[(record.tenant_id, record.company_id, record.verification_id)] = record

    def save(self, record: VerificationRecord) -> VerificationRecord:
        saved = super().save(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = [item.to_dict() for item in self._records.values()]
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            json.dump(serialized, handle, indent=2, sort_keys=True)
            temp_name = handle.name
        os.replace(temp_name, self.path)
        return saved
