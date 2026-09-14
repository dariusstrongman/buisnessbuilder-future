from __future__ import annotations

from abc import ABC, abstractmethod

from businessbuilder.access_broker.models import ProviderConnection

from .models import OAuthTokenMaterial, ProviderCallbackEvent, ProviderReality


class OAuthProviderPort(ABC):
    provider: str
    allowed_scopes: frozenset[str]

    @abstractmethod
    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str,
                          scopes: frozenset[str], nonce: str) -> str: ...

    @abstractmethod
    def exchange_code(self, *, code: str, pkce_verifier: str,
                      redirect_uri: str) -> OAuthTokenMaterial: ...

    @abstractmethod
    def refresh(self, *, refresh_token: bytes) -> OAuthTokenMaterial: ...

    @abstractmethod
    def verify_callback(self, *, body: bytes, signature: str) -> ProviderCallbackEvent: ...

    @abstractmethod
    def reconcile_connection(self, connection: ProviderConnection, *, access_token: bytes) -> ProviderReality: ...

