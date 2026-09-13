from __future__ import annotations

from businessbuilder.runtime import ArtifactRef, Capability, CapabilityFailure, CapabilityRequest, CapabilityResult, FailureKind, Money


SAFE_AGENT_ACTIONS = {
    "role_intake_assistant": frozenset({"classify_lead", "validate_required_fields", "request_missing_information", "draft_acknowledgement"}),
    "role_quote_drafting_assistant": frozenset({"draft_standard_quote", "draft_exception_quote"}),
    "role_inbox_assistant": frozenset({"read_message", "classify_message", "draft_reply", "send_preapproved_reply", "archive_message", "mark_spam"}),
    "role_review_followup_assistant": frozenset({"draft_review_request", "send_preapproved_review_request"}),
}


class DeterministicAgentCapability(Capability):
    """Safe staging adapter: produces an artifact receipt and performs no external action."""

    version = "v1"

    def __init__(self, name: str, *, failure_attempts: int = 0) -> None:
        self.name = name
        self.failure_attempts = failure_attempts
        self.calls: dict[str, int] = {}
        self.results: dict[str, CapabilityResult] = {}
        self.total_calls = 0

    def validate_request(self, request: CapabilityRequest) -> None:
        role = request.inputs.get("agent_role")
        action = request.inputs.get("action")
        if role not in SAFE_AGENT_ACTIONS or action not in SAFE_AGENT_ACTIONS[role]:
            raise PermissionError("bounded agent adapter rejects unapproved role/action")
        if request.inputs.get("model_provider") != "deterministic-test":
            raise PermissionError("staging adapter accepts only the deterministic test provider")

    def estimate(self, request: CapabilityRequest) -> Money:
        return Money("USD", int(request.inputs.get("model_estimated_minor", 0)))

    def execute(self, request: CapabilityRequest) -> CapabilityResult:
        if request.idempotency_key in self.results:
            return self.results[request.idempotency_key]
        self.calls[request.idempotency_key] = self.calls.get(request.idempotency_key, 0) + 1
        self.total_calls += 1
        if self.total_calls <= self.failure_attempts:
            return CapabilityResult(
                f"test_ref_{request.job_id}", "failed", (), Money("USD", 0),
                failure=CapabilityFailure(
                    "TEST_RETRYABLE", "Deterministic staging refusal", True, FailureKind.RETRYABLE
                ),
            )
        result = CapabilityResult(
            f"test_ref_{request.job_id}",
            "succeeded",
            (ArtifactRef("agent_result", f"artifact_{request.job_id}"),),
            self.estimate(request),
            ("bounded_test_execution",),
        )
        self.results[request.idempotency_key] = result
        return result

    def status(self, provider_ref: str) -> str:
        return "succeeded" if any(item.provider_ref == provider_ref for item in self.results.values()) else "unknown"

    def cancel(self, provider_ref: str) -> bool:
        del provider_ref
        return True

    def collect_result(self, provider_ref: str) -> CapabilityResult | None:
        return next((item for item in self.results.values() if item.provider_ref == provider_ref), None)
