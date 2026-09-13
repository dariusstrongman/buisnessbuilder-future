from __future__ import annotations

from businessbuilder.commercial import CommercialEvent, CommercialEventSink, CommercialService
from businessbuilder.runtime.events import LocalEventBus
from businessbuilder.runtime.models import Event


class RuntimeCommercialEventSink(CommercialEventSink):
    """Public adapter: commercial facts become canonical runtime events."""

    def __init__(self, event_bus: LocalEventBus) -> None:
        self.event_bus = event_bus

    def publish(self, event: CommercialEvent) -> bool:
        payload = {**event.payload, "user_id": event.user_id}
        return self.event_bus.publish(
            Event(
                event.event_id,
                event.tenant_id,
                event.company_id,
                event.correlation_id,
                event.causation_id,
                event.event_type,
                event.occurred_at,
                payload,
                event.source,
            )
        )


class RuntimeEntitlementGuard:
    """Fail-closed capability eligibility seam for Core Runtime policy integration."""

    def __init__(self, commercial: CommercialService) -> None:
        self.commercial = commercial

    def require_capability(self, tenant_id: str, company_id: str, capability: str) -> None:
        if not self.commercial.capability_allowed(tenant_id, company_id, capability):
            raise PermissionError(f"active entitlement required for {capability}")
