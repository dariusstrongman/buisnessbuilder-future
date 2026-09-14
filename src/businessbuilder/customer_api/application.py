from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from datetime import datetime, timezone
from http import HTTPStatus
import re
from typing import Any, Callable, Mapping

from businessbuilder.build_room.projection import ScopedEnvelope, project_build_room
from businessbuilder.commercial import Amount, CommercialService
from businessbuilder.commercial.repository import CommercialRepository
from businessbuilder.company_brain import (
    Company,
    CompanyBrainService,
    EntityRef,
    LifecycleState,
    Provenance,
    RecordKind,
    Scope,
)
from businessbuilder.identity import (
    AuthenticatedPrincipal,
    AuthorizationContext,
    AuthorizationDenied,
    IdentityError,
    IdentityAuditEvent,
    IdentityRepository,
    MembershipStatus,
    Permission,
    PrincipalContextAuthority,
    Role,
)
from businessbuilder.integration import CompanyBrainVerificationAdapter
from businessbuilder.runtime.models import ApprovalState
from businessbuilder.runtime.orchestrator import JobOrchestrator
from businessbuilder.runtime.storage import RuntimeRepository
from businessbuilder.verification import ReadinessEvaluator, VerificationService, VerificationState


_COMPANY_ROUTE = re.compile(r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})$")
_COMPANY_CHILD_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/(build-room|founder-actions|readiness|handoff)$"
)
_APPROVAL_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/approvals/([A-Za-z0-9_-]{1,128})$"
)
_ORDER_ROUTE = re.compile(r"^/api/v1/orders/([A-Za-z0-9_-]{1,128})$")
_PROVIDER_CONNECTIONS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/provider-connections$"
)
_PROVIDER_CONNECTION_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/provider-connections/([A-Za-z0-9_-]{1,160})(?:/(disconnect|reconnect))?$"
)
_RECIPIENT_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/recipients/([A-Za-z0-9_.:-]{3,160})(?:/(suppression|opt-out|re-enable))?$"
)
_COMMUNICATION_POLICY_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communication-policy$"
)
_COMMUNICATIONS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communications$"
)
_DELIVERY_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/deliveries/([A-Za-z0-9_.:-]{3,160})$"
)
_COMPLIANCE_STATUS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communications/compliance-status$"
)
_CANARY_READINESS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communications/canary-readiness$"
)
_COMPLIANCE_ALERTS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communications/alerts$"
)
_SEND_STATE_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/communications/send-state$"
)
_CONSENT_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/recipients/([A-Za-z0-9_.:-]{3,160})/consent$"
)
_ERASURE_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/recipients/([A-Za-z0-9_.:-]{3,160})/erasure$"
)
_PUBLIC_UNSUBSCRIBE_ROUTE = "/api/v1/communications/unsubscribe"
_CLEANING_PILOT_START_ROUTE = "/api/v1/pilots/residential-cleaning/intakes"
_CLEANING_PILOT_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/residential-cleaning-pilot(?:/(approve))?$"
)
_CLEANING_FOUNDER_ACTION_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/residential-cleaning-pilot/founder-actions/"
    r"(founder_action_cleaning_[A-Za-z0-9_-]{1,100})(?:/(explain|launch|complete|evidence))?$"
)
_CLEANING_EVIDENCE_SUBMISSIONS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/residential-cleaning-pilot/founder-actions/"
    r"(founder_action_cleaning_[A-Za-z0-9_-]{1,100})/evidence-submissions"
    r"(?:/(cleaning_submission_[A-Za-z0-9_-]{1,100})(?:/(access))?)?$"
)
_CLEANING_EVIDENCE_REVIEWS_ROUTE = re.compile(
    r"^/api/v1/companies/([A-Za-z0-9_-]{1,128})/residential-cleaning-pilot/founder-actions/"
    r"(founder_action_cleaning_[A-Za-z0-9_-]{1,100})/evidence-reviews$"
)
_FORGED_AUTHORITY_HEADERS = frozenset(
    {"x-actor-id", "x-actor-role", "x-user-id", "x-tenant-id", "x-company-id"}
)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: HTTPStatus
    body: dict[str, Any]
    allow: tuple[str, ...] = ()


class ApiFailure(Exception):
    def __init__(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        allow: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.allow = allow


class CustomerApi:
    """Thin API facade over existing Business Builder service authorities."""

    def __init__(
        self,
        *,
        identity_repository: IdentityRepository,
        principal_authority: PrincipalContextAuthority,
        company_brain: CompanyBrainService,
        runtime: JobOrchestrator,
        runtime_repository: RuntimeRepository,
        verification: VerificationService,
        readiness: ReadinessEvaluator,
        company_snapshots: CompanyBrainVerificationAdapter,
        commercial: CommercialService,
        commercial_repository: CommercialRepository,
        id_factory: Callable[[str], str],
        clock: Callable[[], datetime],
        provider_connections=None,
        outbound_communications=None,
        communications_compliance=None,
        live_canary_readiness=None,
        residential_cleaning=None,
    ) -> None:
        self.identity_repository = identity_repository
        self.principal_authority = principal_authority
        self.company_brain = company_brain
        self.runtime = runtime
        self.runtime_repository = runtime_repository
        self.verification = verification
        self.readiness = readiness
        self.company_snapshots = company_snapshots
        self.commercial = commercial
        self.commercial_repository = commercial_repository
        self.id_factory = id_factory
        self.clock = clock
        self.provider_connections = provider_connections
        self.outbound_communications = outbound_communications
        self.communications_compliance = communications_compliance
        self.live_canary_readiness = live_canary_readiness
        self.residential_cleaning = residential_cleaning

    def close(self) -> None:
        """Close unique repository resources owned by the composition root."""
        resources = (
            self.identity_repository,
            self.commercial_repository,
            self.runtime_repository,
            self.verification.repository,
            self.company_brain.repository,
        )
        closed: set[int] = set()
        for resource in resources:
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if close:
                close()

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        query: Mapping[str, list[str]],
        body: object,
        request_id: str,
        correlation_id: str,
    ) -> ApiResponse:
        lowered = {key.lower(): value for key, value in headers.items()}
        raw_token: str | None = None
        actor_id = "unknown"
        try:
            if path == _PUBLIC_UNSUBSCRIBE_ROUTE:
                self._method(method, "POST")
                if self.communications_compliance is None:
                    raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
                values = self._object(body, required={"token"}, allowed={"token"})
                token_value = self._short_string(values["token"], 200)
                # Always return the same short response to avoid a recipient/token oracle.
                self.communications_compliance.unsubscribe(token_value)
                return ApiResponse(HTTPStatus.ACCEPTED, {
                    "status": "accepted",
                    "message": "If the request is valid, future communications are disabled.",
                })
            raw_token = self._bearer(lowered.get("authorization"))
            user = self.principal_authority.authenticate_session(raw_token)
            actor_id = user.user_id
            forged = sorted(name for name in _FORGED_AUTHORITY_HEADERS if lowered.get(name))
            if forged:
                self._audit_denial(
                    actor_id,
                    "unresolved",
                    None,
                    "api.authority_context",
                    "caller supplied forbidden authority headers",
                    request_id,
                    correlation_id,
                )
                raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "request authority is server-derived")
            support_session_id = lowered.get("x-support-impersonation-session") or None
            return self._route(
                method,
                path,
                query,
                body,
                raw_token,
                user,
                support_session_id,
                request_id,
                correlation_id,
            )
        except ApiFailure as exc:
            return ApiResponse(
                exc.status,
                {"status": "error", "error": exc.code, "message": exc.message},
                exc.allow,
            )
        except AuthorizationDenied:
            return ApiResponse(
                HTTPStatus.UNAUTHORIZED,
                {"status": "error", "error": "unauthorized", "message": "active authentication required"},
            )
        except (KeyError, LookupError, IdentityError):
            return ApiResponse(
                HTTPStatus.NOT_FOUND,
                {"status": "error", "error": "not_found", "message": "resource not found"},
            )
        except (TypeError, ValueError):
            return ApiResponse(
                HTTPStatus.BAD_REQUEST,
                {"status": "error", "error": "invalid_request", "message": "request validation failed"},
            )
        except PermissionError:
            return ApiResponse(
                HTTPStatus.FORBIDDEN,
                {"status": "error", "error": "forbidden", "message": "operation is not permitted"},
            )

    def _route(self, method, path, query, body, token, user, support, request_id, correlation_id):
        if path == _CLEANING_PILOT_START_ROUTE:
            self._method(method, "POST")
            if self.residential_cleaning is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            if support:
                raise ApiFailure(
                    HTTPStatus.FORBIDDEN,
                    "forbidden",
                    "support cannot start a founder journey",
                )
            values = self._object(
                body,
                required={"idempotency_key", "intake"},
                allowed={"idempotency_key", "intake"},
            )
            try:
                journey = self.residential_cleaning.start(
                    raw_session_token=token,
                    user=user,
                    intake=values["intake"],
                    idempotency_key=self._short_string(values["idempotency_key"], 160),
                    correlation_id=correlation_id,
                )
            except PermissionError:
                self._audit_denial(
                    user.user_id,
                    "unresolved",
                    None,
                    Permission.APPROVE_FOUNDER_DECISIONS.value,
                    "founder journey admission denied",
                    request_id,
                    correlation_id,
                )
                raise ApiFailure(
                    HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted"
                ) from None
            except RuntimeError:
                raise ApiFailure(
                    HTTPStatus.CONFLICT,
                    "journey_conflict",
                    "journey state conflicts with this request",
                ) from None
            scoped = self._company_principal(
                token,
                user.user_id,
                journey["company"]["company_id"],
                None,
                Permission.VIEW_COMPANY_STATE,
                request_id,
                correlation_id,
            )
            journey["verification"] = self._readiness(scoped)
            return ApiResponse(HTTPStatus.CREATED, {"journey": journey})

        if path == "/api/v1/provider-connections/oauth/callback":
            self._method(method, "POST")
            if self.provider_connections is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            values = self._object(body, required={"state", "code", "pkce_verifier", "redirect_uri"},
                                  allowed={"state", "code", "pkce_verifier", "redirect_uri"})
            # The state selects server-owned scope; no callback tenant/company is accepted.
            connection = self.provider_connections.complete(
                token,
                state=self._short_string(values["state"], 200),
                code=self._short_string(values["code"], 500),
                pkce_verifier=self._short_string(values["pkce_verifier"], 200),
                redirect_uri=self._short_string(values["redirect_uri"], 500),
            )
            return ApiResponse(HTTPStatus.OK, {"provider_connection": self._provider_connection(connection)})

        if path == "/api/v1/me":
            self._method(method, "GET")
            return ApiResponse(HTTPStatus.OK, {"user": self._user(user)})
        if path == "/api/v1/organizations":
            self._method(method, "GET")
            scopes = self._accessible_memberships(token, user.user_id, support)
            organizations = [self._organization(org, principal) for principal, _, org in scopes]
            return ApiResponse(HTTPStatus.OK, {"organizations": organizations})
        if path == "/api/v1/memberships":
            self._method(method, "GET")
            scopes = self._accessible_memberships(token, user.user_id, support)
            return ApiResponse(
                HTTPStatus.OK,
                {"memberships": [self._membership(item, org) for _, item, org in scopes]},
            )
        if path == "/api/v1/companies":
            if method == "GET":
                return ApiResponse(
                    HTTPStatus.OK,
                    {"companies": self._list_companies(token, user.user_id, support, request_id, correlation_id)},
                )
            if method == "POST":
                return ApiResponse(
                    HTTPStatus.CREATED,
                    {"company": self._create_company(token, user.user_id, support, body, request_id, correlation_id)},
                )
            self._method(method, "GET", "POST")

        match = _COMPANY_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            principal = self._company_principal(
                token, user.user_id, match.group(1), support, Permission.VIEW_COMPANY_STATE,
                request_id, correlation_id,
            )
            return ApiResponse(HTTPStatus.OK, {"company": self._company(principal)})

        match = _CLEANING_EVIDENCE_SUBMISSIONS_ROUTE.fullmatch(path)
        if match:
            if self.residential_cleaning is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, action_id, submission_id, access = match.groups()
            principal = self._company_principal(
                token, user.user_id, company_id, support, Permission.ACCESS_ARTIFACTS,
                request_id, correlation_id,
            )
            try:
                if access:
                    self._method(method, "POST")
                    values = self._object(body, allowed=frozenset())
                    del values
                    result = self.residential_cleaning.issue_evidence_access(
                        principal, action_id=action_id, submission_id=submission_id or ""
                    )
                    return ApiResponse(HTTPStatus.OK, {"evidence_access": result})
                if submission_id:
                    self._method(method, "GET")
                    state = self.residential_cleaning.evidence_state(principal, action_id=action_id)
                    item = next(
                        (candidate for candidate in state["submissions"]
                         if candidate["submission_id"] == submission_id),
                        None,
                    )
                    if item is None:
                        raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
                    return ApiResponse(HTTPStatus.OK, {"evidence_submission": item})
                if method == "GET":
                    state = self.residential_cleaning.evidence_state(principal, action_id=action_id)
                    return ApiResponse(HTTPStatus.OK, {"evidence_submissions": state["submissions"]})
                self._method(method, "POST")
                values = self._object(
                    body,
                    required={"idempotency_key", "source", "evidence_type"},
                    allowed={
                        "idempotency_key", "source", "evidence_type", "filename",
                        "content_type", "content_base64", "reference",
                        "supersedes_submission_id",
                    },
                )
                source = self._short_string(values["source"], 80)
                common = {
                    "action_id": action_id,
                    "evidence_type": self._short_string(values["evidence_type"], 80),
                    "idempotency_key": self._short_string(values["idempotency_key"], 160),
                    "supersedes_submission_id": (
                        self._identifier(values["supersedes_submission_id"], "supersedes_submission_id")
                        if values.get("supersedes_submission_id") is not None else None
                    ),
                }
                if source == "file_upload":
                    if not {"filename", "content_type", "content_base64"}.issubset(values) or "reference" in values:
                        raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "file evidence fields are invalid")
                    encoded = values["content_base64"]
                    if not isinstance(encoded, str) or len(encoded) > 720_000:
                        raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "encoded evidence is too large")
                    try:
                        content = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "evidence encoding is invalid") from None
                    submission = self.residential_cleaning.submit_evidence_upload(
                        principal,
                        filename=self._short_string(values["filename"], 120),
                        content_type=self._short_string(values["content_type"], 100),
                        content=content,
                        **common,
                    )
                else:
                    if "reference" not in values or any(
                        item in values for item in {"filename", "content_type", "content_base64"}
                    ):
                        raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "reference evidence fields are invalid")
                    if not isinstance(values["reference"], dict):
                        raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "reference must be an object")
                    submission = self.residential_cleaning.submit_evidence_reference(
                        principal, source=source, reference=dict(values["reference"]), **common
                    )
                return ApiResponse(HTTPStatus.CREATED, {"evidence_submission": submission})
            except PermissionError:
                self._audit_denial(
                    principal.user_id, principal.tenant_id, company_id,
                    Permission.ACCESS_ARTIFACTS.value, "evidence submission or access denied",
                    request_id, correlation_id,
                )
                raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted") from None
            except RuntimeError:
                raise ApiFailure(HTTPStatus.CONFLICT, "evidence_conflict", "evidence state conflicts with this request") from None

        match = _CLEANING_EVIDENCE_REVIEWS_ROUTE.fullmatch(path)
        if match:
            if self.residential_cleaning is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, action_id = match.groups()
            principal = self._company_principal(
                token, user.user_id, company_id, support, Permission.ACCESS_ARTIFACTS,
                request_id, correlation_id,
            )
            try:
                if method == "GET":
                    state = self.residential_cleaning.evidence_state(principal, action_id=action_id)
                    return ApiResponse(HTTPStatus.OK, {"evidence_reviews": state["reviews"]})
                self._method(method, "POST")
                values = self._object(
                    body,
                    required={"idempotency_key", "submission_ids", "decision", "reason_code"},
                    allowed={
                        "idempotency_key", "submission_ids", "decision", "reason_code",
                        "operator_notes", "requested_additional_evidence", "supersedes_review_id",
                    },
                )
                if not isinstance(values["submission_ids"], list) or not all(
                    isinstance(item, str) for item in values["submission_ids"]
                ):
                    raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "submission_ids must be a list")
                requested = values.get("requested_additional_evidence")
                if requested is not None and (
                    not isinstance(requested, list) or not all(isinstance(item, str) for item in requested)
                ):
                    raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "requested evidence must be a list")
                result = self.residential_cleaning.review_evidence(
                    principal,
                    action_id=action_id,
                    submission_ids=[self._identifier(item, "submission_id") for item in values["submission_ids"]],
                    decision=self._short_string(values["decision"], 80),
                    reason_code=self._short_string(values["reason_code"], 80),
                    idempotency_key=self._short_string(values["idempotency_key"], 160),
                    operator_notes=(
                        self._short_string(values["operator_notes"], 500)
                        if values.get("operator_notes") is not None else None
                    ),
                    requested_additional_evidence=requested,
                    supersedes_review_id=(
                        self._identifier(values["supersedes_review_id"], "supersedes_review_id")
                        if values.get("supersedes_review_id") is not None else None
                    ),
                )
                return ApiResponse(HTTPStatus.OK, result)
            except PermissionError:
                self._audit_denial(
                    principal.user_id, principal.tenant_id, company_id,
                    Permission.ACCESS_ARTIFACTS.value, "operator evidence review denied",
                    request_id, correlation_id,
                )
                raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted") from None
            except RuntimeError:
                raise ApiFailure(HTTPStatus.CONFLICT, "review_conflict", "review state conflicts with this request") from None

        match = _CLEANING_FOUNDER_ACTION_ROUTE.fullmatch(path)
        if match:
            if self.residential_cleaning is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, action_id, operation = match.groups()
            permission = (
                Permission.APPROVE_FOUNDER_DECISIONS
                if operation is not None
                else Permission.VIEW_COMPANY_STATE
            )
            principal = self._company_principal(
                token,
                user.user_id,
                company_id,
                support,
                permission,
                request_id,
                correlation_id,
            )
            if operation is None:
                self._method(method, "GET")
                journey = self.residential_cleaning.project(principal)
                action = next(
                    (
                        item
                        for item in journey["founder_actions"]
                        if item["founder_action_id"] == action_id
                    ),
                    None,
                )
                if action is None:
                    raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
                return ApiResponse(HTTPStatus.OK, {"founder_action": action})
            self._method(method, "POST")
            if operation == "evidence":
                if getattr(self.residential_cleaning, "evidence_reviews", None) is not None:
                    raise ApiFailure(
                        HTTPStatus.CONFLICT,
                        "evidence_submission_required",
                        "use the customer-safe evidence submission and operator review workflow",
                    )
                values = self._object(
                    body,
                    required={"idempotency_key", "evidence_refs"},
                    allowed={"idempotency_key", "evidence_refs"},
                )
                evidence_refs = values["evidence_refs"]
                if not isinstance(evidence_refs, list):
                    raise ApiFailure(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "evidence_refs must be a list",
                    )
            else:
                values = self._object(
                    body,
                    required={"idempotency_key"},
                    allowed={"idempotency_key"},
                )
                evidence_refs = None
            runtime_operation = {
                "explain": "explain",
                "launch": "launch",
                "complete": "founder_complete",
                "evidence": "capture_result",
            }[operation]
            try:
                action = self.residential_cleaning.transition_founder_action(
                    principal,
                    action_id=action_id,
                    operation=runtime_operation,
                    idempotency_key=self._short_string(values["idempotency_key"], 160),
                    evidence_refs=evidence_refs,
                )
            except PermissionError:
                self._audit_denial(
                    principal.user_id,
                    principal.tenant_id,
                    company_id,
                    permission.value,
                    "founder action transition denied",
                    request_id,
                    correlation_id,
                )
                raise ApiFailure(
                    HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted"
                ) from None
            except RuntimeError:
                raise ApiFailure(
                    HTTPStatus.CONFLICT,
                    "journey_conflict",
                    "founder action state conflicts with this request",
                ) from None
            return ApiResponse(HTTPStatus.OK, {"founder_action": action})

        match = _CLEANING_PILOT_ROUTE.fullmatch(path)
        if match:
            if self.residential_cleaning is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, action = match.groups()
            permission = (
                Permission.APPROVE_FOUNDER_DECISIONS
                if action == "approve"
                else Permission.VIEW_COMPANY_STATE
            )
            principal = self._company_principal(
                token,
                user.user_id,
                company_id,
                support,
                permission,
                request_id,
                correlation_id,
            )
            try:
                if action == "approve":
                    self._method(method, "POST")
                    values = self._object(
                        body, required={"approval_id"}, allowed={"approval_id"}
                    )
                    journey = self.residential_cleaning.approve(
                        principal,
                        approval_id=self._identifier(values["approval_id"], "approval_id"),
                    )
                else:
                    self._method(method, "GET")
                    journey = self.residential_cleaning.project(principal)
            except PermissionError:
                self._audit_denial(
                    principal.user_id,
                    principal.tenant_id,
                    company_id,
                    permission.value,
                    "founder journey operation denied",
                    request_id,
                    correlation_id,
                )
                raise ApiFailure(
                    HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted"
                ) from None
            except RuntimeError:
                raise ApiFailure(
                    HTTPStatus.CONFLICT,
                    "journey_conflict",
                    "journey state conflicts with this request",
                ) from None
            journey["verification"] = self._readiness(principal)
            return ApiResponse(HTTPStatus.OK, {"journey": journey})

        match = _PROVIDER_CONNECTIONS_ROUTE.fullmatch(path)
        if match:
            if self.provider_connections is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            permission = (Permission.VIEW_COMPANY_STATE if method == "GET"
                          else Permission.MANAGE_PROVIDER_CONNECTIONS)
            principal = self._company_principal(token, user.user_id, company_id, support,
                permission, request_id, correlation_id)
            if method == "GET":
                values = self.provider_connections.list_connections(
                    principal, tenant_id=principal.tenant_id, company_id=company_id
                )
                return ApiResponse(HTTPStatus.OK, {"provider_connections": [
                    self._provider_connection(item) for item in values
                ]})
            self._method(method, "GET", "POST")
            values = self._object(body, required={"provider", "redirect_uri", "scopes"},
                                  allowed={"provider", "redirect_uri", "scopes"})
            scopes = values["scopes"]
            if not isinstance(scopes, list) or not scopes or any(not isinstance(item, str) for item in scopes):
                raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "scopes must be a non-empty list")
            started = self.provider_connections.start(
                principal, tenant_id=principal.tenant_id, company_id=company_id,
                provider_name=self._identifier(values["provider"], "provider"),
                redirect_uri=self._short_string(values["redirect_uri"], 500),
                scopes=frozenset(scopes),
            )
            return ApiResponse(HTTPStatus.CREATED, {"authorization": {
                "provider_connection_id": started.connection_id,
                "authorization_url": started.authorization_url,
                "pkce_verifier": started.pkce_verifier,
                "expires_at": self._time(started.expires_at),
            }})

        match = _PROVIDER_CONNECTION_ROUTE.fullmatch(path)
        if match:
            if self.provider_connections is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, connection_id, action = match.groups()
            permission = Permission.VIEW_COMPANY_STATE if not action else Permission.MANAGE_PROVIDER_CONNECTIONS
            principal = self._company_principal(token, user.user_id, company_id, support,
                permission, request_id, correlation_id)
            if action == "disconnect":
                self._method(method, "POST")
                connection = self.provider_connections.disconnect(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    connection_id=connection_id,
                )
            elif action == "reconnect":
                self._method(method, "POST")
                values = self._object(body, required={"redirect_uri", "scopes"},
                                      allowed={"redirect_uri", "scopes"})
                current = self.provider_connections.get_connection(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    connection_id=connection_id,
                )
                scopes = values["scopes"]
                if not isinstance(scopes, list) or not scopes:
                    raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "scopes must be a non-empty list")
                started = self.provider_connections.start(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    provider_name=current.provider,
                    redirect_uri=self._short_string(values["redirect_uri"], 500),
                    scopes=frozenset(scopes), reconnect_connection_id=connection_id,
                )
                return ApiResponse(HTTPStatus.OK, {"authorization": {
                    "provider_connection_id": started.connection_id,
                    "authorization_url": started.authorization_url,
                    "pkce_verifier": started.pkce_verifier,
                    "expires_at": self._time(started.expires_at),
                }})
            else:
                self._method(method, "GET")
                connection = self.provider_connections.get_connection(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    connection_id=connection_id,
                )
            return ApiResponse(HTTPStatus.OK, {"provider_connection": self._provider_connection(connection)})

        match = _RECIPIENT_ROUTE.fullmatch(path)
        if match:
            if self.outbound_communications is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, recipient_id, action = match.groups()
            permission = Permission.MANAGE_COMMUNICATIONS if (
                action in {"opt-out", "re-enable"}
                or (action == "suppression" and method == "POST")
            ) else Permission.VIEW_COMPANY_STATE
            principal = self._company_principal(token, user.user_id, company_id, support,
                permission, request_id, correlation_id)
            if action == "opt-out":
                self._method(method, "POST")
                values = self._object(body, required={"event_id"}, allowed={"event_id", "reason"})
                recipient = self.outbound_communications.opt_out(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    recipient_id=recipient_id,
                    event_id=self._identifier(values["event_id"], "event_id"),
                    reason=self._identifier(values.get("reason", "customer_request"), "reason"),
                )
            elif action == "re-enable":
                self._method(method, "POST")
                values = self._object(body, required={"consent_provenance"}, allowed={"consent_provenance"})
                recipient = self.outbound_communications.explicitly_reenable(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    recipient_id=recipient_id,
                    consent_provenance=self._identifier(values["consent_provenance"], "consent_provenance"),
                )
            elif action == "suppression" and method == "POST":
                from businessbuilder.outbound_communications import SuppressionReason
                values = self._object(body, required={"event_id", "reason"},
                                      allowed={"event_id", "reason"})
                recipient = self.outbound_communications.suppress(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    recipient_id=recipient_id,
                    event_id=self._identifier(values["event_id"], "event_id"),
                    reason=SuppressionReason(self._identifier(values["reason"], "reason")),
                )
            else:
                self._method(method, "GET")
                recipient = self.outbound_communications.get_recipient(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    recipient_id=recipient_id,
                )
            if action == "suppression":
                return ApiResponse(HTTPStatus.OK, {"suppression": self._suppression(recipient)})
            return ApiResponse(HTTPStatus.OK, {"recipient": self._recipient(recipient)})

        match = _COMMUNICATION_POLICY_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.outbound_communications is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            value = self.outbound_communications.policy_status(
                principal, tenant_id=principal.tenant_id, company_id=company_id)
            return ApiResponse(HTTPStatus.OK, {"communication_policy": value})

        match = _COMPLIANCE_STATUS_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.communications_compliance is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            return ApiResponse(HTTPStatus.OK, {"communications":
                self.communications_compliance.customer_status(
                    principal, tenant_id=principal.tenant_id, company_id=company_id)})

        match = _CANARY_READINESS_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.live_canary_readiness is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            return ApiResponse(HTTPStatus.OK, {"communications":
                self.live_canary_readiness.customer_status(
                    principal, tenant_id=principal.tenant_id, company_id=company_id)})

        match = _COMPLIANCE_ALERTS_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.communications_compliance is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            alerts = self.communications_compliance.list_alerts(
                principal, tenant_id=principal.tenant_id, company_id=company_id)
            return ApiResponse(HTTPStatus.OK, {"alerts": [self._compliance_alert(v) for v in alerts]})

        match = _SEND_STATE_ROUTE.fullmatch(path)
        if match:
            if self.communications_compliance is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            permission = Permission.MANAGE_COMMUNICATIONS if method == "POST" else Permission.VIEW_COMPANY_STATE
            principal = self._company_principal(token, user.user_id, company_id, support,
                permission, request_id, correlation_id)
            if method == "POST":
                values = self._object(body, required={"paused", "reason_code"},
                                      allowed={"paused", "reason_code"})
                if not isinstance(values["paused"], bool):
                    raise ValueError("paused must be boolean")
                self.communications_compliance.set_company_kill_switch(
                    principal, tenant_id=principal.tenant_id, company_id=company_id,
                    engaged=values["paused"],
                    reason_code=self._identifier(values["reason_code"], "reason_code"))
            else:
                self._method(method, "GET")
            return ApiResponse(HTTPStatus.OK, {"communications":
                self.communications_compliance.customer_status(
                    principal, tenant_id=principal.tenant_id, company_id=company_id)})

        match = _CONSENT_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.communications_compliance is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, recipient_id = match.groups()
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            values = self.communications_compliance.list_consent(
                principal, tenant_id=principal.tenant_id, company_id=company_id,
                recipient_id=recipient_id)
            return ApiResponse(HTTPStatus.OK, {"consent": [self._consent_summary(v) for v in values]})

        match = _ERASURE_ROUTE.fullmatch(path)
        if match:
            self._method(method, "POST")
            if self.communications_compliance is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, recipient_id = match.groups()
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.MANAGE_COMMUNICATIONS, request_id, correlation_id)
            values = self._object(body, required={"reason"}, allowed={"reason"})
            result = self.communications_compliance.request_erasure(
                principal, tenant_id=principal.tenant_id, company_id=company_id,
                recipient_id=recipient_id,
                reason=self._identifier(values["reason"], "reason"))
            return ApiResponse(HTTPStatus.ACCEPTED, {"erasure": {
                "erasure_id": result.erasure_id,
                "completed": result.completed_at is not None,
                "operational_pii_removed": result.operational_pii_removed,
                "compliance_evidence_preserved": result.compliance_evidence_preserved,
                "blocked_by_hold": result.blocked_by_hold,
            }})

        match = _COMMUNICATIONS_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.outbound_communications is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id = match.group(1)
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            values = self.outbound_communications.list_deliveries(
                principal, tenant_id=principal.tenant_id, company_id=company_id)
            return ApiResponse(HTTPStatus.OK, {"communications": [self._delivery(item) for item in values]})

        match = _DELIVERY_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            if self.outbound_communications is None:
                raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
            company_id, delivery_id = match.groups()
            principal = self._company_principal(token, user.user_id, company_id, support,
                Permission.VIEW_COMPANY_STATE, request_id, correlation_id)
            value = self.outbound_communications.get_delivery(
                principal, tenant_id=principal.tenant_id, company_id=company_id,
                delivery_id=delivery_id)
            return ApiResponse(HTTPStatus.OK, {"delivery": self._delivery(value)})

        match = _COMPANY_CHILD_ROUTE.fullmatch(path)
        if match:
            company_id, child = match.groups()
            if child == "handoff" and method == "POST":
                self._company_principal(
                    token, user.user_id, company_id, support, Permission.PERFORM_HANDOFF,
                    request_id, correlation_id,
                )
                raise ApiFailure(
                    HTTPStatus.CONFLICT,
                    "handoff_not_available",
                    "handoff requires a dedicated authoritative workflow",
                )
            self._method(method, "GET")
            permission = Permission.ACCESS_ARTIFACTS if child == "build-room" else Permission.VIEW_COMPANY_STATE
            principal = self._company_principal(
                token, user.user_id, company_id, support, permission,
                request_id, correlation_id,
            )
            if child == "build-room":
                return ApiResponse(HTTPStatus.OK, {"build_room": self._build_room(principal)})
            if child == "founder-actions":
                return ApiResponse(HTTPStatus.OK, {"founder_actions": self._founder_actions(principal)})
            if child == "readiness":
                return ApiResponse(HTTPStatus.OK, {"readiness": self._readiness(principal)})
            return ApiResponse(HTTPStatus.OK, {"handoff": self._handoff(principal)})

        match = _APPROVAL_ROUTE.fullmatch(path)
        if match:
            self._method(method, "POST")
            company_id, approval_id = match.groups()
            principal = self._company_principal(
                token, user.user_id, company_id, support, Permission.VIEW_COMPANY_STATE,
                request_id, correlation_id,
            )
            values = self._object(body, allowed={"decision"})
            if values.get("decision", "granted") != "granted":
                raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "only explicit granted decisions are supported")
            approval = self.runtime_repository.get_approval(
                principal.tenant_id, company_id, approval_id
            )
            if approval is None:
                self._deny(principal, Permission.APPROVE_FOUNDER_DECISIONS, "approval scope mismatch", request_id, correlation_id)
            job = self.runtime.approve_job(
                tenant_id=principal.tenant_id,
                company_id=company_id,
                job_id=approval.job_id,
                approval_id=approval_id,
                principal=principal,
            )
            decided = self.runtime_repository.get_approval(
                principal.tenant_id, company_id, approval_id
            )
            return ApiResponse(HTTPStatus.OK, {"approval": self._approval(decided), "job_status": job.status.value})

        if path == "/api/v1/orders":
            if method == "POST":
                values = self._object(body, required={"company_id", "product_version_id"}, allowed={"company_id", "product_version_id"})
                company_id = self._identifier(values["company_id"], "company_id")
                principal = self._company_principal(
                    token, user.user_id, company_id, support, Permission.AUTHORIZE_SPEND,
                    request_id, correlation_id,
                )
                order = self.commercial.create_order(
                    self._context(principal),
                    self._identifier(values["product_version_id"], "product_version_id"),
                )
                return ApiResponse(HTTPStatus.CREATED, {"order": self._order(order)})
            if method == "GET":
                principal = self._query_company(
                    query, token, user.user_id, support, Permission.VIEW_BILLING,
                    request_id, correlation_id,
                )
                orders = self.commercial_repository.list_current_orders(
                    principal.tenant_id, principal.company_id or ""
                )
                return ApiResponse(HTTPStatus.OK, {"orders": [self._order(item) for item in orders]})
            self._method(method, "GET", "POST")

        match = _ORDER_ROUTE.fullmatch(path)
        if match:
            self._method(method, "GET")
            principal = self._query_company(
                query, token, user.user_id, support, Permission.VIEW_BILLING,
                request_id, correlation_id,
            )
            order = self.commercial_repository.get_order(
                principal.tenant_id, principal.company_id or "", match.group(1)
            )
            return ApiResponse(HTTPStatus.OK, {"order": self._order(order)})

        if path in {"/api/v1/subscriptions", "/api/v1/entitlements"}:
            self._method(method, "GET")
            permission = Permission.VIEW_BILLING if path.endswith("subscriptions") else Permission.VIEW_COMPANY_STATE
            principal = self._query_company(
                query, token, user.user_id, support, permission, request_id, correlation_id
            )
            if path.endswith("subscriptions"):
                values = self.commercial_repository.list_current_subscriptions(
                    principal.tenant_id, principal.company_id or ""
                )
                return ApiResponse(HTTPStatus.OK, {"subscriptions": [self._subscription(item) for item in values]})
            values = self.commercial_repository.get_current_entitlement_grants(
                principal.tenant_id, principal.company_id or ""
            )
            return ApiResponse(HTTPStatus.OK, {"entitlements": [self._entitlement(item) for item in values]})

        raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")

    @staticmethod
    def _bearer(value: str | None) -> str:
        if not value or len(value) > 4096:
            raise AuthorizationDenied("bearer token required")
        scheme, separator, token = value.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not token or " " in token:
            raise AuthorizationDenied("valid bearer token required")
        return token

    @staticmethod
    def _method(actual: str, *allowed: str) -> None:
        if actual not in allowed:
            raise ApiFailure(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "method not allowed",
                allow=tuple(allowed),
            )

    @staticmethod
    def allowed_methods(path: str) -> tuple[str, ...]:
        """Return only methods supported by a recognized customer route."""
        if path == _CLEANING_PILOT_START_ROUTE:
            return ("POST",)
        cleaning_pilot = _CLEANING_PILOT_ROUTE.fullmatch(path)
        if cleaning_pilot:
            return ("POST",) if cleaning_pilot.group(2) == "approve" else ("GET",)
        cleaning_action = _CLEANING_FOUNDER_ACTION_ROUTE.fullmatch(path)
        if cleaning_action:
            return ("POST",) if cleaning_action.group(3) else ("GET",)
        cleaning_submission = _CLEANING_EVIDENCE_SUBMISSIONS_ROUTE.fullmatch(path)
        if cleaning_submission:
            if cleaning_submission.group(4):
                return ("POST",)
            return ("GET",) if cleaning_submission.group(3) else ("GET", "POST")
        if _CLEANING_EVIDENCE_REVIEWS_ROUTE.fullmatch(path):
            return ("GET", "POST")
        if path == "/api/v1/companies" or path == "/api/v1/orders":
            return ("GET", "POST")
        child = _COMPANY_CHILD_ROUTE.fullmatch(path)
        if child:
            return ("GET", "POST") if child.group(2) == "handoff" else ("GET",)
        if _APPROVAL_ROUTE.fullmatch(path):
            return ("POST",)
        if path == "/api/v1/provider-connections/oauth/callback":
            return ("POST",)
        if path == _PUBLIC_UNSUBSCRIBE_ROUTE:
            return ("POST",)
        if _PROVIDER_CONNECTIONS_ROUTE.fullmatch(path):
            return ("GET", "POST")
        provider = _PROVIDER_CONNECTION_ROUTE.fullmatch(path)
        if provider:
            return ("POST",) if provider.group(3) else ("GET",)
        recipient = _RECIPIENT_ROUTE.fullmatch(path)
        if recipient:
            if recipient.group(3) == "suppression":
                return ("GET", "POST")
            return ("POST",) if recipient.group(3) in {"opt-out", "re-enable"} else ("GET",)
        if (_COMMUNICATION_POLICY_ROUTE.fullmatch(path)
                or _COMMUNICATIONS_ROUTE.fullmatch(path)
                or _DELIVERY_ROUTE.fullmatch(path)
                or _COMPLIANCE_STATUS_ROUTE.fullmatch(path)
                or _CANARY_READINESS_ROUTE.fullmatch(path)
                or _COMPLIANCE_ALERTS_ROUTE.fullmatch(path)
                or _CONSENT_ROUTE.fullmatch(path)):
            return ("GET",)
        if _SEND_STATE_ROUTE.fullmatch(path):
            return ("GET", "POST")
        if _ERASURE_ROUTE.fullmatch(path):
            return ("POST",)
        if (
            path in {
                "/api/v1/me",
                "/api/v1/organizations",
                "/api/v1/memberships",
                "/api/v1/subscriptions",
                "/api/v1/entitlements",
            }
            or _COMPANY_ROUTE.fullmatch(path)
            or _ORDER_ROUTE.fullmatch(path)
        ):
            return ("GET",)
        return ()

    @staticmethod
    def _object(body: object, *, required=frozenset(), allowed=frozenset()) -> dict[str, Any]:
        if body is None:
            body = {}
        if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "request body has invalid fields")
        return dict(body)

    @staticmethod
    def _identifier(value: object, field: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", f"{field} is invalid")
        return value

    @staticmethod
    def _short_string(value: object, maximum: int) -> str:
        if not isinstance(value, str) or not value or len(value) > maximum or any(c in value for c in "\r\n\0"):
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "request field is invalid")
        return value

    def _accessible_memberships(self, token, user_id, support):
        values = []
        for membership in self.identity_repository.list_user_memberships(user_id):
            if membership.status is not MembershipStatus.ACTIVE:
                continue
            try:
                organization = self.identity_repository.get_organization(membership.organization_id)
                company_id = None
                support_id = None
                if membership.role is Role.SUPPORT:
                    if not support:
                        continue
                    session = self.identity_repository.get_support_session(support)
                    if session.tenant_id != membership.tenant_id:
                        continue
                    company_id = session.company_id
                    support_id = support
                principal = self.principal_authority.issue(
                    token,
                    tenant_id=membership.tenant_id,
                    company_id=company_id,
                    support_impersonation_session_id=support_id,
                )
                values.append((principal, membership, organization))
            except (IdentityError, LookupError):
                continue
        return tuple(values)

    def _company_principal(self, token, user_id, company_id, support, permission, request_id, correlation_id):
        candidate_tenants: list[str] = []
        for membership in self.identity_repository.list_user_memberships(user_id):
            if membership.status is not MembershipStatus.ACTIVE:
                continue
            try:
                organization = self.identity_repository.get_organization(membership.organization_id)
            except LookupError:
                continue
            if company_id in organization.company_ids:
                candidate_tenants.append(membership.tenant_id)
        if len(set(candidate_tenants)) != 1:
            self._audit_denial(user_id, "unresolved", company_id, permission.value, "company scope mismatch", request_id, correlation_id)
            raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")
        tenant_id = candidate_tenants[0]
        try:
            principal = self.principal_authority.issue(
                token,
                tenant_id=tenant_id,
                company_id=company_id,
                support_impersonation_session_id=support,
            )
            self.commercial.authorization.require(
                self._context(principal), permission, at=self.clock()
            )
            return principal
        except (IdentityError, LookupError) as exc:
            self._audit_denial(user_id, tenant_id, company_id, permission.value, type(exc).__name__, request_id, correlation_id)
            raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "operation is not permitted") from None

    def _query_company(self, query, token, user_id, support, permission, request_id, correlation_id):
        values = query.get("company_id", [])
        if len(values) != 1:
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "one company_id selector is required")
        company_id = self._identifier(values[0], "company_id")
        return self._company_principal(token, user_id, company_id, support, permission, request_id, correlation_id)

    def _deny(self, principal, permission, reason, request_id, correlation_id):
        self._audit_denial(principal.user_id, principal.tenant_id, principal.company_id, permission.value, reason, request_id, correlation_id)
        raise ApiFailure(HTTPStatus.NOT_FOUND, "not_found", "resource not found")

    def _audit_denial(self, actor_id, tenant_id, company_id, permission, reason, request_id, correlation_id):
        self.identity_repository.append_audit(
            IdentityAuditEvent(
                self.id_factory("identity_audit"),
                tenant_id,
                actor_id,
                "authorization.denied",
                "permission",
                permission,
                self.clock(),
                reason,
                "businessbuilder.customer_api",
                company_id,
                {"request_id": request_id, "correlation_id": correlation_id},
            )
        )

    @staticmethod
    def _context(principal):
        return AuthorizationContext(
            principal.user_id,
            principal.tenant_id,
            principal.company_id,
            principal.support_impersonation_session_id,
        )

    def _list_companies(self, token, user_id, support, request_id, correlation_id):
        results = []
        seen = set()
        for _, _, organization in self._accessible_memberships(token, user_id, support):
            for company_id in organization.company_ids:
                if company_id in seen:
                    continue
                try:
                    principal = self._company_principal(
                        token, user_id, company_id, support, Permission.VIEW_COMPANY_STATE,
                        request_id, correlation_id,
                    )
                    results.append(self._company(principal))
                    seen.add(company_id)
                except ApiFailure:
                    continue
        return results

    def _create_company(self, token, user_id, support, body, request_id, correlation_id):
        values = self._object(
            body,
            required={"display_name", "archetype", "jurisdiction"},
            allowed={"display_name", "legal_name", "archetype", "jurisdiction", "organization_id"},
        )
        if support:
            raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "support cannot create companies")
        choices = [
            (principal, membership, organization)
            for principal, membership, organization in self._accessible_memberships(token, user_id, None)
            if membership.role is Role.OWNER
            and (not values.get("organization_id") or organization.organization_id == values["organization_id"])
        ]
        if len(choices) != 1:
            self._audit_denial(user_id, "unresolved", None, Permission.APPROVE_FOUNDER_DECISIONS.value, "organization scope mismatch", request_id, correlation_id)
            raise ApiFailure(HTTPStatus.FORBIDDEN, "forbidden", "one owned organization is required")
        principal, _, _ = choices[0]
        display_name = values["display_name"]
        archetype = values["archetype"]
        jurisdiction = values["jurisdiction"]
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 200:
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "display_name is invalid")
        if not isinstance(archetype, str) or not archetype.strip() or len(archetype) > 100:
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "archetype is invalid")
        if not isinstance(jurisdiction, dict) or len(jurisdiction) > 20:
            raise ApiFailure(HTTPStatus.BAD_REQUEST, "invalid_request", "jurisdiction is invalid")
        company_id = self.id_factory("company")
        stamp = self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        company = Company(
            Scope(principal.tenant_id, company_id),
            display_name.strip(),
            archetype.strip(),
            jurisdiction,
            (EntityRef("user", principal.user_id),),
            lifecycle=LifecycleState.DRAFT,
            legal_name=(values.get("legal_name") or None),
            provenance=(
                Provenance(
                    "authenticated_customer",
                    stamp,
                    EntityRef("user", principal.user_id),
                    source_ref=f"request:{request_id}",
                ),
            ),
        )
        self.company_brain.create_company(company)
        from businessbuilder.identity import IdentityService
        identity = IdentityService(
            self.identity_repository, id_factory=self.id_factory, clock=self.clock
        )
        identity.attach_company(
            AuthorizationContext(principal.user_id, principal.tenant_id), company_id
        )
        scoped = self.principal_authority.issue(token, tenant_id=principal.tenant_id, company_id=company_id)
        return self._company(scoped)

    def _company(self, principal):
        company = self.company_brain.get_company(Scope(principal.tenant_id, principal.company_id or ""))
        return {
            "company_id": company.scope.company_id,
            "display_name": company.display_name,
            "legal_name": company.legal_name,
            "archetype": company.archetype,
            "jurisdiction": dict(company.jurisdiction),
            "lifecycle": company.lifecycle.value,
            "version": company.version,
        }

    @staticmethod
    def _user(user):
        return {
            "user_id": user.user_id,
            "email": user.email,
            "status": user.status.value,
            "email_verified": user.email_verified_at is not None,
        }

    @staticmethod
    def _organization(organization, principal):
        return {
            "organization_id": organization.organization_id,
            "display_name": organization.display_name,
            "status": organization.status.value,
            "role": principal.role.value,
        }

    @staticmethod
    def _membership(item, organization):
        return {
            "membership_id": item.membership_id,
            "organization_id": organization.organization_id,
            "organization_name": organization.display_name,
            "role": item.role.value,
            "status": item.status.value,
        }

    @staticmethod
    def _order(order):
        return {
            "order_id": order.order_id,
            "company_id": order.company_id,
            "status": order.status.value,
            "items": [
                {
                    "product_code": item.product_code.value,
                    "product_version_id": item.product_version_id,
                    "package_name": item.package_name_snapshot,
                    "billing_mode": item.billing_mode.value,
                    "quantity": item.quantity,
                }
                for item in order.items
            ],
            "total": CustomerApi._amount(order.total),
            "created_at": CustomerApi._time(order.created_at),
            "updated_at": CustomerApi._time(order.updated_at),
            "version": order.version,
        }

    @staticmethod
    def _subscription(item):
        return {
            "subscription_id": item.subscription_id,
            "company_id": item.company_id,
            "order_id": item.order_id,
            "product_code": item.plan.product_code.value,
            "status": item.status.value,
            "renewal_state": item.renewal_state.value,
            "period": {"starts_at": CustomerApi._time(item.billing_period.starts_at), "ends_at": CustomerApi._time(item.billing_period.ends_at)},
            "version": item.version,
        }

    @staticmethod
    def _entitlement(item):
        return {
            "entitlement_id": item.grant_id,
            "company_id": item.company_id,
            "code": item.entitlement_code,
            "class": item.entitlement_class.value,
            "status": item.status.value,
            "effective_until": CustomerApi._time(item.effective_until),
            "version": item.version,
        }

    @staticmethod
    def _provider_connection(item):
        return {
            "provider_connection_id": item.connection_id,
            "company_id": item.company_id,
            "provider": item.provider,
            "account_type": item.account_type,
            "provider_account_id": item.provider_account_id,
            "scopes_requested": sorted(item.scopes_requested),
            "scopes_granted": sorted(item.scopes_granted),
            "auth_method": item.auth_method,
            "status": item.status.value,
            "connected_at": CustomerApi._time(item.connected_at),
            "expires_at": CustomerApi._time(item.expires_at),
            "refreshed_at": CustomerApi._time(item.refreshed_at),
            "revoked_at": CustomerApi._time(item.revoked_at),
        }

    @staticmethod
    def _recipient(item):
        local, _, domain = item.normalized_destination.partition("@")
        hint = (local[:1] + "***@" + domain) if local and domain else "hidden"
        return {
            "recipient_id": item.recipient_id,
            "destination_type": item.destination_type.value,
            "destination_hint": hint,
            "relationship": item.relationship.value,
            "consent_state": item.consent_state.value,
            "consent_at": CustomerApi._time(item.consent_at),
            "suppression_state": item.suppression_state.value,
            "opt_out_at": CustomerApi._time(item.opt_out_at),
            "last_contacted_at": CustomerApi._time(item.last_contacted_at),
            "updated_at": CustomerApi._time(item.updated_at),
        }

    @staticmethod
    def _suppression(item):
        return {
            "recipient_id": item.recipient_id,
            "state": item.suppression_state.value,
            "reason": item.suppression_reason,
            "opt_out_at": CustomerApi._time(item.opt_out_at),
            "updated_at": CustomerApi._time(item.updated_at),
        }

    @staticmethod
    def _delivery(item):
        return {
            "delivery_id": item.delivery_id,
            "communication_id": item.communication_id,
            "recipient_id": item.recipient_id,
            "provider": item.provider,
            "purpose": item.purpose.value,
            "status": item.status.value,
            "response_classification": item.response_classification,
            "reconciliation_status": item.reconciliation_status,
            "created_at": CustomerApi._time(item.created_at),
            "updated_at": CustomerApi._time(item.updated_at),
        }

    @staticmethod
    def _consent_summary(item):
        return {
            "consent_evidence_id": item.consent_evidence_id,
            "purpose": item.purpose.value if item.purpose else "contextual",
            "channel": item.channel.value,
            "consent_basis": item.consent_basis.value,
            "source": item.source,
            "captured_at": CustomerApi._time(item.captured_at),
            "policy_version": item.policy_version,
            "terms_version": item.terms_version,
            "expires_at": CustomerApi._time(item.expires_at),
            "withdrawn_at": CustomerApi._time(item.withdrawn_at),
            "superseded": item.superseded_by_id is not None,
        }

    @staticmethod
    def _compliance_alert(item):
        return {
            "alert_id": item.alert_id,
            "class": item.alert_class.value,
            "severity": item.severity.value,
            "reason_code": item.reason_code,
            "created_at": CustomerApi._time(item.created_at),
            "acknowledged_at": CustomerApi._time(item.acknowledged_at),
        }

    @staticmethod
    def _approval(item):
        return {
            "approval_id": item.approval_id,
            "company_id": item.company_id,
            "state": item.state.value,
            "required_role": item.required_role,
            "decided_by": item.decided_by,
            "decided_by_role": item.decided_by_role,
            "expires_at": CustomerApi._time(item.expires_at),
            "version": item.version,
        }

    def _readiness(self, principal):
        snapshot = self.company_snapshots.get_snapshot(principal.tenant_id, principal.company_id or "")
        value = self.readiness.evaluate(snapshot, at=self.clock())
        return {
            "authority": "verification",
            "ready": value.ready,
            "fully_set": value.fully_set,
            "unmet_ready": list(value.unmet_ready),
            "unmet_fully_set": list(value.unmet_fully_set),
            "blocking_ids": list(value.blocking_ids),
            "evaluated_at": self._time(value.evaluated_at),
        }

    def _founder_actions(self, principal):
        records = self.company_brain.query_current_state(
            Scope(principal.tenant_id, principal.company_id or ""), kinds=(RecordKind.FOUNDER_ACTION,)
        )
        allowed = {
            "title", "reason", "instructions", "risk", "irreversible", "state",
            "required_evidence_kinds", "evidence_refs", "due_at", "critical",
            "selected", "responsibility", "partner_authority", "authority_target",
            "blocks", "approval_id",
            "prepared_data", "destination", "evidence", "verification", "timestamps",
            "last_actor", "history", "action_key",
        }
        values = [
            {"founder_action_id": item.record_id, **{key: value for key, value in item.data.items() if key in allowed}, "version": item.version}
            for item in records
        ]
        evidence_reviews = getattr(self.residential_cleaning, "evidence_reviews", None)
        if evidence_reviews is not None:
            for item in values:
                if item["founder_action_id"].startswith("founder_action_cleaning_"):
                    item["evidence_review"] = evidence_reviews.public_action_state(
                        principal.tenant_id,
                        principal.company_id or "",
                        item["founder_action_id"],
                    )
        return values

    def _handoff(self, principal):
        records = self.verification.list_for_company(principal.tenant_id, principal.company_id or "")
        handoff = next((item for item in records if item.definition_id == "handoff.complete"), None)
        verified = bool(handoff and handoff.state is VerificationState.VERIFIED and handoff.is_current(self.clock()))
        return {
            "company_id": principal.company_id,
            "state": "verified" if verified else "incomplete",
            "authority": "verification",
            "verification_id": handoff.verification_id if handoff else None,
        }

    def _build_room(self, principal):
        tenant_id, company_id = principal.tenant_id, principal.company_id or ""
        company = self._company(principal)
        internal_company = {"tenant_id": tenant_id, **company}
        readiness = self._readiness(principal)
        jobs = self.runtime_repository.list_jobs(tenant_id, company_id)
        approvals = self.runtime_repository.list_approvals(tenant_id, company_id)
        verifications = self.verification.list_for_company(tenant_id, company_id)
        evidence = {
            item.evidence_id: item
            for verification in verifications
            for item in verification.evidence
        }
        job_by_id = {item.job_id: item for item in jobs}
        budgets = self.runtime_repository.list_budgets(tenant_id, company_id)
        budget = budgets[0] if budgets else None
        projected = project_build_room(
            generated_at=self._time(self.clock()),
            source=ScopedEnvelope(tenant_id, company_id, {"company": "company_brain", "execution": "runtime", "readiness": "verification"}),
            company=internal_company,
            readiness=ScopedEnvelope(tenant_id, company_id, readiness),
            budget=ScopedEnvelope(
                tenant_id,
                company_id,
                {
                    "currency": budget.ceiling.currency if budget else "USD",
                    "ceiling_minor": budget.ceiling.minor_units if budget else 0,
                    "reserved_minor": budget.reserved_minor if budget else 0,
                    "settled_minor": budget.settled_minor if budget else 0,
                },
            ),
            jobs=(
                {
                    "tenant_id": tenant_id, "company_id": company_id, "id": item.job_id,
                    "kind": "job", "title": item.capability.replace(".", " ").title(),
                    "description": str(item.inputs.get("objective", "")), "owner": "Runtime",
                    "status": item.contract_status(), "dependency_ids": list(item.dependency_ids),
                    "cost_minor": item.settled_minor, "evidence_refs": [], "order": index,
                }
                for index, item in enumerate(jobs, 1)
            ),
            verifications=(
                {
                    "tenant_id": tenant_id, "company_id": company_id, "id": item.verification_id,
                    "kind": "verification", "title": item.definition_id.replace(".", " ").title(),
                    "description": item.scope, "owner": item.owner or "Verification",
                    "status": item.state.value, "dependency_ids": [dep.dependency_id for dep in item.dependencies],
                    "cost_minor": 0, "evidence_refs": [f"artifact:{entry.artifact_ref}" for entry in item.evidence],
                    "order": index + len(jobs),
                }
                for index, item in enumerate(verifications, 1)
            ),
            approvals=(
                {
                    "tenant_id": tenant_id, "company_id": company_id, "approval_id": item.approval_id,
                    "title": "Approval required", "summary": "Review the exact Runtime subject before deciding.",
                    "state": item.state.value, "subject_digest": item.subject_digest,
                    "subject_ref": f"job:{item.job_id}", "required_approver_role": item.required_role,
                    "requested_at": self._time(job_by_id[item.job_id].created_at) if item.job_id in job_by_id else self._time(self.clock()),
                    "decided_at": None, "expires_at": self._time(item.expires_at),
                }
                for item in approvals
            ),
            evidence=(
                {
                    "tenant_id": tenant_id, "company_id": company_id, "evidence_id": item.evidence_id,
                    "title": item.evidence_type.value.replace("_", " ").title(), "evidence_type": item.evidence_type.value,
                    "artifact_ref": item.artifact_ref, "captured_at": self._time(item.captured_at),
                    "expires_at": self._time(item.expires_at), "issuer": item.issuer,
                    "test_name": item.test_name, "test_passed": item.test_passed,
                }
                for item in evidence.values()
            ),
            founder_actions=(
                {"tenant_id": tenant_id, "company_id": company_id, "order": index, **item}
                for index, item in enumerate(self._founder_actions(principal), 1)
            ),
            blockers=(),
            events=(),
            handoff=ScopedEnvelope(tenant_id, company_id, self._handoff(principal)),
        ).to_dict()
        return self._remove_internal_scope(projected)

    @staticmethod
    def _remove_internal_scope(value):
        if isinstance(value, dict):
            internal = {
                "tenant_id",
                "user_id",
                "token_digest",
                "provider_ref",
                "payment_intent_ref",
                "checkout_intent_id",
                "subject_digest",
                "provenance",
            }
            return {
                key: CustomerApi._remove_internal_scope(item)
                for key, item in value.items()
                if key not in internal
            }
        if isinstance(value, list):
            return [CustomerApi._remove_internal_scope(item) for item in value]
        return value

    @staticmethod
    def _amount(value: Amount | None):
        return {"currency": value.currency, "minor_units": value.minor_units} if value else None

    @staticmethod
    def _time(value):
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
