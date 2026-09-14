from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import secrets
from threading import RLock
from urllib.parse import urlencode

from businessbuilder.access_broker.models import ProviderActionResult, ProviderConnection, ReceiptStatus
from businessbuilder.access_broker.ports import EphemeralArtifact, EphemeralSecret, ExternalProviderPort
from businessbuilder.outbound_communications.models import DeliveryStatus, VerifiedDeliveryEvent

from .models import OAuthTokenMaterial, ProviderCallbackEvent, ProviderFailureClass, ProviderReality
from .ports import OAuthProviderPort


class SandboxProviderError(RuntimeError):
    def __init__(self, message: str, classification: ProviderFailureClass) -> None:
        super().__init__(message)
        self.classification = classification


class SandboxEmailProvider(OAuthProviderPort, ExternalProviderPort):
    """Deterministic, process-local OAuth/email provider; it cannot contact a network."""

    provider = "sandbox-email"
    allowed_scopes = frozenset({"mail.read", "mail.send"})
    allowed_operations = frozenset({
        "read_message", "classify_message", "draft_reply", "send_preapproved_reply",
        "draft_review_request", "send_preapproved_review_request", "attachment_metadata",
    })

    def __init__(self, *, clock=lambda: datetime.now(timezone.utc)) -> None:
        self.clock = clock
        self._codes: dict[str, tuple[str, str, frozenset[str]]] = {}
        self._tokens: dict[bytes, tuple[str, frozenset[str], bool]] = {}
        self._requests: dict[str, ProviderActionResult] = {}
        self._callback_key = secrets.token_bytes(32)
        self._lock = RLock()
        self.action_count = 0
        self.refresh_count = 0

    def authorization_url(self, *, state, code_challenge, redirect_uri, scopes, nonce) -> str:
        if not scopes or not scopes <= self.allowed_scopes:
            raise PermissionError("requested OAuth scope is not allowed")
        return "https://sandbox-email.invalid/oauth/authorize?" + urlencode({
            "state": state, "code_challenge": code_challenge,
            "code_challenge_method": "S256", "redirect_uri": redirect_uri,
            "scope": " ".join(sorted(scopes)), "nonce": nonce,
        })

    def issue_test_code(self, *, code_challenge: str, redirect_uri: str,
                        scopes: frozenset[str]) -> str:
        code = "sandbox_code_" + secrets.token_urlsafe(18)
        self._codes[code] = (code_challenge, redirect_uri, scopes)
        return code

    def exchange_code(self, *, code, pkce_verifier, redirect_uri) -> OAuthTokenMaterial:
        try:
            challenge, expected_redirect, scopes = self._codes.pop(code)
        except KeyError as exc:
            raise SandboxProviderError("authorization code is invalid", ProviderFailureClass.RECONNECT_REQUIRED) from exc
        actual = sha256(pkce_verifier.encode()).digest()
        import base64
        actual_challenge = base64.urlsafe_b64encode(actual).rstrip(b"=").decode()
        if not hmac.compare_digest(challenge, actual_challenge) or redirect_uri != expected_redirect:
            raise SandboxProviderError("PKCE or redirect validation failed", ProviderFailureClass.PERMANENTLY_DENIED)
        return self._new_tokens(scopes)

    def _new_tokens(self, scopes: frozenset[str]) -> OAuthTokenMaterial:
        access = secrets.token_bytes(32)
        refresh = secrets.token_bytes(32)
        account = "sandbox_mailbox_001"
        self._tokens[access] = (account, scopes, True)
        self._tokens[refresh] = (account, scopes, True)
        return OAuthTokenMaterial(access_token=access, refresh_token=refresh,
            expires_at=self.clock() + timedelta(minutes=20), scopes=scopes,
            provider_account_id=account, safe_metadata=(("environment", "sandbox"),))

    def refresh(self, *, refresh_token: bytes) -> OAuthTokenMaterial:
        with self._lock:
            current = self._tokens.get(refresh_token)
            if current is None or not current[2]:
                raise SandboxProviderError("refresh token requires reconnect", ProviderFailureClass.RECONNECT_REQUIRED)
            self.refresh_count += 1
            return self._new_tokens(current[1])

    def execute(self, *, operation, provider_request_id, idempotency_key,
                credential: EphemeralSecret, artifacts: tuple[EphemeralArtifact, ...]) -> ProviderActionResult:
        del artifacts
        if operation not in self.allowed_operations:
            raise PermissionError("sandbox email operation is not allowed")
        def valid(raw):
            try:
                material = OAuthTokenMaterial.decode_from_vault(bytes(raw), provider_account_id="sandbox_mailbox_001")
                try:
                    return self._tokens.get(material.access_token(), (None, None, False))[2]
                finally:
                    material.close()
            except Exception:
                return False
        if not credential.use(valid):
            raise SandboxProviderError("provider token is revoked or malformed", ProviderFailureClass.RECONNECT_REQUIRED)
        with self._lock:
            prior = self._requests.get(idempotency_key)
            if prior:
                return prior
            self.action_count += 1
            result = ProviderActionResult(ReceiptStatus.SUCCEEDED, "sandbox_action_accepted",
                                          f"sandbox-object:{provider_request_id}", False)
            self._requests[idempotency_key] = result
            return result

    def reconcile(self, provider_request_id: str) -> ProviderActionResult | None:
        return next((item for item in self._requests.values()
                     if item.external_object_ref == f"sandbox-object:{provider_request_id}"), None)

    def revoke_tokens(self) -> None:
        self._tokens = {token: (account, scopes, False) for token, (account, scopes, _) in self._tokens.items()}

    def sign_callback(self, body: bytes) -> str:
        return hmac.new(self._callback_key, body, sha256).hexdigest()

    def verify_callback(self, *, body: bytes, signature: str) -> ProviderCallbackEvent:
        expected = self.sign_callback(body)
        if not signature or not hmac.compare_digest(expected, signature):
            raise PermissionError("provider callback signature is invalid")
        data = json.loads(body)
        allowed = {"event_id", "connection_id", "event_type", "occurred_at", "reason"}
        if not isinstance(data, dict) or set(data) - allowed or set(data) != allowed:
            raise ValueError("provider callback is malformed")
        return ProviderCallbackEvent(
            event_id=str(data["event_id"]), provider=self.provider,
            connection_id=str(data["connection_id"]), event_type=str(data["event_type"]),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"]).replace("Z", "+00:00")),
            safe_reason=str(data["reason"])[:120],
        )

    def delivery_callback(self, *, event_id: str, provider_request_id: str,
                          status: DeliveryStatus, classification: str = "sandbox_delivery") -> tuple[bytes, str]:
        body = json.dumps({
            "event_id": event_id,
            "provider_request_id": provider_request_id,
            "status": status.value,
            "occurred_at": self.clock().isoformat().replace("+00:00", "Z"),
            "response_classification": classification,
        }, sort_keys=True, separators=(",", ":")).encode()
        return body, self.sign_callback(body)

    def verify_delivery_callback(self, *, body: bytes, signature: str) -> VerifiedDeliveryEvent:
        expected = self.sign_callback(body)
        if not signature or not hmac.compare_digest(expected, signature):
            raise PermissionError("provider delivery callback signature is invalid")
        data = json.loads(body)
        allowed = {"event_id", "provider_request_id", "status", "occurred_at", "response_classification"}
        if not isinstance(data, dict) or set(data) != allowed:
            raise ValueError("provider delivery callback is malformed")
        return VerifiedDeliveryEvent(
            event_id=str(data["event_id"]), provider=self.provider,
            provider_request_id=str(data["provider_request_id"]),
            status=DeliveryStatus(str(data["status"])),
            occurred_at=datetime.fromisoformat(str(data["occurred_at"]).replace("Z", "+00:00")),
            response_classification=str(data["response_classification"])[:120],
        )

    def reconcile_connection(self, connection, *, access_token: bytes) -> ProviderReality:
        value = self._tokens.get(access_token)
        if value is None or not value[2]:
            return ProviderReality(False, connection.provider_account_id, frozenset(),
                                   failure_class=ProviderFailureClass.RECONNECT_REQUIRED)
        return ProviderReality(True, value[0], value[1], (("environment", "sandbox"),))
