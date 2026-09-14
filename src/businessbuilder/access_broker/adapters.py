from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import secrets
from threading import RLock

from .models import ProviderActionResult, ReceiptStatus
from .ports import ArtifactStorePort, EphemeralArtifact, EphemeralSecret, ExternalProviderPort, SecretStorePort


class InMemorySecretStore(SecretStorePort):
    """Test adapter. Values are supplied at runtime and never serialized."""

    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    def register(self, locator: str, value: bytes) -> None:
        self._values[locator] = bytes(value)

    def store(self, locator: str, value: bytes) -> None:
        self.register(locator, value)

    def revoke(self, locator: str) -> None:
        value = self._values.pop(locator, None)
        if value is not None:
            scratch = bytearray(value)
            for index in range(len(scratch)):
                scratch[index] = 0

    def resolve(self, locator: str) -> EphemeralSecret:
        try:
            return EphemeralSecret(self._values[locator])
        except KeyError as exc:
            raise LookupError("secret reference cannot be resolved") from exc


class AwsSecretsManagerStore(SecretStorePort):
    """Exact-ARN Secrets Manager adapter using the ambient task-role chain."""

    def __init__(self, *, allowed_secret_arns: frozenset[str], client=None) -> None:
        if not allowed_secret_arns or any("*" in arn for arn in allowed_secret_arns):
            raise ValueError("Secrets Manager allowlist must contain exact ARNs")
        if client is None:
            import boto3
            client = boto3.client("secretsmanager")
        self.client = client
        self.allowed_secret_arns = allowed_secret_arns

    def resolve(self, locator: str) -> EphemeralSecret:
        if locator not in self.allowed_secret_arns:
            raise PermissionError("secret locator is outside the worker allowlist")
        response = self.client.get_secret_value(SecretId=locator)
        if "SecretBinary" in response:
            value = bytes(response["SecretBinary"])
        else:
            value = str(response["SecretString"]).encode("utf-8")
        return EphemeralSecret(value)

    def store(self, locator: str, value: bytes) -> None:
        if locator not in self.allowed_secret_arns:
            raise PermissionError("secret locator is outside the worker allowlist")
        self.client.put_secret_value(SecretId=locator, SecretBinary=bytes(value))

    def revoke(self, locator: str) -> None:
        if locator not in self.allowed_secret_arns:
            raise PermissionError("secret locator is outside the worker allowlist")
        # Replace provider material with a non-sensitive tombstone while durable
        # connection state supplies the immediate authorization denial.
        self.client.put_secret_value(SecretId=locator, SecretString='{"status":"revoked"}')


class InMemoryArtifactStore(ArtifactStorePort):
    def __init__(self, *, clock=lambda: datetime.now(timezone.utc)) -> None:
        self._objects: dict[str, bytes] = {}
        self._tokens: dict[str, tuple[str, datetime]] = {}
        self.clock = clock

    def put(self, object_key: str, content: bytes, *, content_type: str, content_sha256: str) -> None:
        del content_type
        if sha256(content).hexdigest() != content_sha256:
            raise ValueError("artifact upload hash mismatch")
        self._objects[object_key] = bytes(content)

    def read(self, object_key: str, *, maximum_bytes: int) -> EphemeralArtifact:
        try:
            value = self._objects[object_key]
        except KeyError as exc:
            raise LookupError("artifact object does not exist") from exc
        if len(value) > maximum_bytes:
            raise ValueError("artifact object exceeds authorized size")
        return EphemeralArtifact(value)

    def signed_download(self, object_key: str, *, expires_in_seconds: int) -> str:
        if object_key not in self._objects:
            raise LookupError("artifact object does not exist")
        token = secrets.token_urlsafe(24)
        self._tokens[token] = (object_key, self.clock() + timedelta(seconds=expires_in_seconds))
        return f"memory-signed://{token}"

    def fetch_signed(self, url: str) -> bytes:
        token = url.removeprefix("memory-signed://")
        try:
            key, expires_at = self._tokens[token]
        except KeyError as exc:
            raise PermissionError("signed artifact access is invalid") from exc
        if self.clock() >= expires_at:
            raise PermissionError("signed artifact access expired")
        return self._objects[key]

    def tamper_for_test(self, object_key: str, value: bytes) -> None:
        self._objects[object_key] = bytes(value)


class S3ArtifactStore(ArtifactStorePort):
    """Private, no-list S3 adapter constrained to one configured key prefix."""

    def __init__(self, *, bucket: str, allowed_prefix: str, client=None) -> None:
        if not bucket or not allowed_prefix or "*" in allowed_prefix:
            raise ValueError("S3 bucket and exact allowed prefix are required")
        if not allowed_prefix.endswith("/"):
            allowed_prefix += "/"
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client = client
        self.bucket = bucket
        self.allowed_prefix = allowed_prefix

    def _authorize(self, object_key: str) -> None:
        if not object_key.startswith(self.allowed_prefix) or ".." in object_key:
            raise PermissionError("S3 object key is outside the broker prefix")

    def put(self, object_key: str, content: bytes, *, content_type: str, content_sha256: str) -> None:
        self._authorize(object_key)
        if sha256(content).hexdigest() != content_sha256:
            raise ValueError("artifact upload hash mismatch")
        self.client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=content,
            ContentType=content_type,
            Metadata={"content-sha256": content_sha256},
            ServerSideEncryption="AES256",
        )

    def read(self, object_key: str, *, maximum_bytes: int) -> EphemeralArtifact:
        self._authorize(object_key)
        response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        size = int(response.get("ContentLength", 0))
        if size > maximum_bytes:
            response["Body"].close()
            raise ValueError("artifact object exceeds authorized size")
        value = response["Body"].read(maximum_bytes + 1)
        response["Body"].close()
        if len(value) > maximum_bytes:
            raise ValueError("artifact object exceeds authorized size")
        return EphemeralArtifact(value)

    def signed_download(self, object_key: str, *, expires_in_seconds: int) -> str:
        self._authorize(object_key)
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": object_key},
            ExpiresIn=expires_in_seconds,
        )


class DeterministicExternalProvider(ExternalProviderPort):
    """Safe provider-shaped adapter: records calls and performs no external action."""

    def __init__(self, provider: str = "test-email") -> None:
        self.provider = provider
        self.calls: dict[str, ProviderActionResult] = {}
        self.call_count = 0
        self._lock = RLock()

    def execute(
        self,
        *,
        operation: str,
        provider_request_id: str,
        idempotency_key: str,
        credential: EphemeralSecret,
        artifacts: tuple[EphemeralArtifact, ...],
    ) -> ProviderActionResult:
        del artifacts
        if not credential.use(lambda value: bool(value)):
            raise PermissionError("provider credential is empty")
        with self._lock:
            prior = self.calls.get(idempotency_key)
            if prior:
                return prior
            self.call_count += 1
            result = ProviderActionResult(
                ReceiptStatus.SUCCEEDED,
                "accepted_test_action",
                f"test-object:{provider_request_id}",
                False,
            )
            self.calls[idempotency_key] = result
            return result

    def reconcile(self, provider_request_id: str) -> ProviderActionResult | None:
        return next(
            (result for result in self.calls.values() if result.external_object_ref == f"test-object:{provider_request_id}"),
            None,
        )
