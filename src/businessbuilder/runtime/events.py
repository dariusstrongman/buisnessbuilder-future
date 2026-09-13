from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from .audit import AuditLog
from .models import Event
from .storage import RuntimeRepository


EventHandler = Callable[[Event], None]


class LocalEventBus:
    """Synchronous, ordered, at-least-once-safe local/dev dispatcher."""

    def __init__(self, repository: RuntimeRepository, audit: AuditLog) -> None:
        self.repository = repository
        self.audit = audit
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> bool:
        inserted = self.repository.append_event(event)
        if not inserted:
            return False
        self.audit.record(
            tenant_id=event.tenant_id,
            company_id=event.company_id,
            actor_type="system",
            actor_id="runtime_events",
            action="event.recorded",
            target_type="event",
            target_id=event.event_id,
            correlation_id=event.correlation_id,
            reason=f"Recorded canonical event {event.type}",
            after=event.to_contract(),
        )
        for handler in tuple(self._handlers.get(event.type, ())) + tuple(self._handlers.get("*", ())):
            handler(event)
        return True
