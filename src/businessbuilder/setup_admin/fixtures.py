from __future__ import annotations

from datetime import datetime, timezone

from .catalog import default_catalog
from .models import Provenance
from .repository import InMemorySetupAdminRepository
from .service import SetupAdminService


FIXED_NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


def billy_bob_setup() -> tuple[SetupAdminService, tuple[object, ...]]:
    """Fictional, offline mobile lawn-service setup fixture."""
    service = SetupAdminService(InMemorySetupAdminRepository(), default_catalog())
    items = service.create_plan(
        "tenant_billy",
        "co_billy_bob_lawn",
        "mobile_service",
        locality="Denton County, Texas",
        provenance=Provenance(
            source_type="founder",
            source_ref="fixture://billy-bob/setup-intake",
            captured_at=FIXED_NOW,
            actor_type="founder",
            actor_id="party_billy",
            notes="Fictional offline fixture; no authority determination.",
        ),
        idempotency_key="billy-setup-plan-0001",
        correlation_id="correlation_billy_setup",
        at=FIXED_NOW,
    )
    return service, items
