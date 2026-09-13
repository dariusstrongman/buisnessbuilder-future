from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
from typing import Any, Protocol
from uuid import uuid4

from .models import QueueDelivery, assert_queue_payload_safe


class QueuePort(Protocol):
    def send(self, message_id: str, payload: dict[str, Any]) -> str: ...
    def receive(self, *, wait_seconds: int, visibility_timeout: int) -> QueueDelivery | None: ...
    def acknowledge(self, receipt_handle: str) -> None: ...
    def release(self, receipt_handle: str, *, delay_seconds: int = 0) -> None: ...
    def renew(self, receipt_handle: str, *, visibility_timeout: int) -> None: ...


@dataclass
class _MemoryMessage:
    message_id: str
    payload: dict[str, Any]
    receipt_handle: str
    receive_count: int = 0
    visible: bool = True


class InMemoryQueue:
    """Deterministic at-least-once queue adapter used only by tests."""

    def __init__(self, *, max_receive_count: int = 3) -> None:
        self.messages: deque[_MemoryMessage] = deque()
        self.dead_letters: list[_MemoryMessage] = []
        self.max_receive_count = max_receive_count

    def send(self, message_id: str, payload: dict[str, Any]) -> str:
        assert_queue_payload_safe(payload)
        self.messages.append(_MemoryMessage(message_id, payload, f"receipt_{uuid4().hex}"))
        return message_id

    def receive(self, *, wait_seconds: int = 0, visibility_timeout: int = 60) -> QueueDelivery | None:
        del wait_seconds, visibility_timeout
        for message in tuple(self.messages):
            if not message.visible:
                continue
            message.receive_count += 1
            if message.receive_count > self.max_receive_count:
                self.messages.remove(message)
                self.dead_letters.append(message)
                continue
            message.visible = False
            return QueueDelivery(
                message.message_id,
                message.receipt_handle,
                dict(message.payload),
                message.receive_count,
            )
        return None

    def acknowledge(self, receipt_handle: str) -> None:
        for message in tuple(self.messages):
            if message.receipt_handle == receipt_handle:
                self.messages.remove(message)
                return

    def release(self, receipt_handle: str, *, delay_seconds: int = 0) -> None:
        del delay_seconds
        for message in self.messages:
            if message.receipt_handle == receipt_handle:
                message.visible = True
                return

    def renew(self, receipt_handle: str, *, visibility_timeout: int) -> None:
        del visibility_timeout
        if not any(item.receipt_handle == receipt_handle for item in self.messages):
            raise LookupError("queue receipt not found")

    def redeliver_all(self) -> None:
        for message in self.messages:
            message.visible = True


class SqsQueue:
    """AWS SQS adapter. Credentials come only from the task role/provider chain."""

    def __init__(self, queue_url: str, *, client=None) -> None:
        if not queue_url.startswith("https://sqs."):
            raise ValueError("an AWS SQS queue URL is required")
        if client is None:
            import boto3

            client = boto3.client("sqs")
        self.queue_url = queue_url
        self.client = client

    def send(self, message_id: str, payload: dict[str, Any]) -> str:
        assert_queue_payload_safe(payload)
        body = json.dumps(
            {"message_id": message_id, "envelope": payload},
            sort_keys=True,
            separators=(",", ":"),
        )
        response = self.client.send_message(QueueUrl=self.queue_url, MessageBody=body)
        return response["MessageId"]

    def receive(self, *, wait_seconds: int = 20, visibility_timeout: int = 60) -> QueueDelivery | None:
        response = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            VisibilityTimeout=max(0, min(visibility_timeout, 43200)),
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", ())
        if not messages:
            return None
        message = messages[0]
        body = json.loads(message["Body"])
        return QueueDelivery(
            body["message_id"],
            message["ReceiptHandle"],
            body["envelope"],
            int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1")),
        )

    def acknowledge(self, receipt_handle: str) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    def release(self, receipt_handle: str, *, delay_seconds: int = 0) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=max(0, min(delay_seconds, 43200)),
        )

    def renew(self, receipt_handle: str, *, visibility_timeout: int) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=max(1, min(visibility_timeout, 43200)),
        )
