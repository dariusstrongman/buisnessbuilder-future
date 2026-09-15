"""Private, company-scoped pilot admission; never treats waived fees as payment."""

from __future__ import annotations

from datetime import timedelta
import hmac
import json
from typing import Protocol


PILOT_SECRET_NAME = "business-builder/pilot-access"
PILOT_SECRET_FIELD = "promo_code"


class PilotSecretUnavailable(RuntimeError):
    pass


class PilotCodeReader(Protocol):
    def read_code(self) -> str: ...


class AwsPilotCodeReader:
    """Reads only the exact pilot secret at request time using the task role."""

    def __init__(self, client=None) -> None:
        self._client = client

    def read_code(self) -> str:
        try:
            if self._client is None:
                import boto3
                self._client = boto3.client("secretsmanager")
            response = self._client.get_secret_value(SecretId=PILOT_SECRET_NAME)
            payload = json.loads(response["SecretString"])
            value = payload[PILOT_SECRET_FIELD]
            if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 128:
                raise ValueError("invalid pilot secret shape")
            return value
        except Exception:
            # Never propagate AWS responses or secret payloads into API/log output.
            raise PilotSecretUnavailable("pilot access unavailable") from None


def code_matches(submitted: str, configured: str) -> bool:
    if not isinstance(submitted, str) or not 1 <= len(submitted.encode("utf-8")) <= 128:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), configured.encode("utf-8"))


INVALID_ATTEMPT_WINDOW = timedelta(minutes=15)
INVALID_ATTEMPT_LIMIT = 5
