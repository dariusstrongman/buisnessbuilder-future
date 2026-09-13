from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .ports import CommercialEventSink
from .repository import CommercialRepository


class CommercialOutboxDispatcher:
    """Lease-based delivery for commercial events already committed to an outbox."""

    def __init__(
        self,
        repository: CommercialRepository,
        sink: CommercialEventSink,
        *,
        dispatcher_id: str,
        clock: Callable[[], datetime],
        lease: timedelta = timedelta(seconds=30),
        retry_delay: timedelta = timedelta(seconds=1),
    ) -> None:
        if not dispatcher_id:
            raise ValueError("dispatcher_id is required")
        self.repository = repository
        self.sink = sink
        self.dispatcher_id = dispatcher_id
        self.clock = clock
        self.lease = lease
        self.retry_delay = retry_delay

    def dispatch_pending(self, *, limit: int = 100) -> int:
        delivered = 0
        while delivered < limit:
            claimed = self.repository.claim_outbox(
                self.dispatcher_id,
                at=self.clock(),
                lease=self.lease,
                limit=1,
            )
            if not claimed:
                break
            message = claimed[0]
            try:
                self.sink.publish(message.event)
            except Exception as exc:
                self.repository.release_outbox(
                    message.outbox_id,
                    self.dispatcher_id,
                    retry_at=self.clock() + self.retry_delay,
                    error=type(exc).__name__,
                )
                raise
            self.repository.acknowledge_outbox(
                message.outbox_id, self.dispatcher_id, at=self.clock()
            )
            delivered += 1
        return delivered
