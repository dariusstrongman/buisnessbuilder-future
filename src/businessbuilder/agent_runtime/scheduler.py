from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

from businessbuilder.runtime import Event

from .models import RuntimeSchedule


class RuntimeScheduler:
    """Durable due-work scanner; it emits events but never executes jobs."""

    def __init__(self, repository, event_bus, *, clock) -> None:
        self.repository = repository
        self.event_bus = event_bus
        self.clock = clock

    def save(self, schedule: RuntimeSchedule) -> None:
        self.repository.save_runtime_schedule(schedule)

    def tick(self, *, limit: int = 100) -> tuple[Event, ...]:
        now = self.clock()
        emitted = []
        for schedule in self.repository.list_due_runtime_schedules(at=now, limit=limit):
            occurrence = schedule.next_due_at.isoformat()
            event = Event(
                f"event_schedule_{schedule.schedule_id}_{int(schedule.next_due_at.timestamp())}",
                schedule.tenant_id,
                schedule.company_id,
                f"schedule:{schedule.schedule_id}:{occurrence}",
                schedule.schedule_id,
                "runtime.schedule.due",
                now,
                {
                    "schedule_id": schedule.schedule_id,
                    "role_id": schedule.role_id,
                    "capability": schedule.capability,
                    "action": schedule.action,
                    "due_at": occurrence,
                },
                "businessbuilder.runtime.scheduler",
            )
            if self.event_bus.publish(event):
                emitted.append(event)
            next_due = schedule.next_due_at
            while next_due <= now:
                next_due += timedelta(seconds=schedule.interval_seconds)
            self.repository.save_runtime_schedule(replace(schedule, next_due_at=next_due))
        return tuple(emitted)


class EventBridgeSchedulerAdapter:
    """Production-shaped AWS adapter that carries only an opaque schedule reference."""

    def __init__(self, *, group_name: str, client=None) -> None:
        if client is None:
            import boto3

            client = boto3.client("scheduler")
        self.group_name = group_name
        self.client = client

    def upsert(self, schedule: RuntimeSchedule, *, target_arn: str, role_arn: str) -> None:
        if schedule.interval_seconds % 60:
            raise ValueError("EventBridge schedule intervals must use whole minutes")
        payload = json.dumps(
            {
                "schema_version": "runtime-schedule-trigger.v1",
                "schedule_id": schedule.schedule_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        minutes = schedule.interval_seconds // 60
        unit = "minute" if minutes == 1 else "minutes"
        expression = f"rate({minutes} {unit})"
        request = {
            "Name": schedule.schedule_id,
            "GroupName": self.group_name,
            "ScheduleExpression": expression,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "State": "ENABLED" if schedule.enabled else "DISABLED",
            "Target": {"Arn": target_arn, "RoleArn": role_arn, "Input": payload},
        }
        try:
            self.client.update_schedule(**request)
        except self.client.exceptions.ResourceNotFoundException:
            self.client.create_schedule(**request)
