from .adapters import SandboxEmailProvider, SandboxProviderError
from .bootstrap import attach_provider_connections
from .models import (
    AuthorizationStart, OAuthTokenMaterial, OAuthTransaction, ProviderCallbackEvent,
    ProviderFailureClass, ProviderHealth, ProviderReality,
    classify_provider_failure,
)
from .ports import OAuthProviderPort
from .service import OAuthFlowDenied, ProviderConnectionService, ROLE_OPERATION_SCOPES

__all__ = [
    "AuthorizationStart", "OAuthFlowDenied", "OAuthProviderPort", "OAuthTokenMaterial",
    "OAuthTransaction", "ProviderCallbackEvent", "ProviderConnectionService",
    "ProviderFailureClass", "ProviderHealth", "ProviderReality",
    "classify_provider_failure",
    "ROLE_OPERATION_SCOPES", "SandboxEmailProvider", "SandboxProviderError",
    "attach_provider_connections",
]
