from __future__ import annotations

from dataclasses import dataclass

from .models import EvidenceType, VerificationMethod, VerificationState


@dataclass(frozen=True)
class VerificationDefinition:
    definition_id: str
    description: str
    allowed_methods: frozenset[VerificationMethod]
    defined_tests: frozenset[str]
    required_evidence_types: frozenset[EvidenceType]
    invalidation_state: VerificationState = VerificationState.EXECUTED
    default_ttl_seconds: int | None = None


class VerificationDefinitionRegistry:
    def __init__(self, definitions: tuple[VerificationDefinition, ...] = ()) -> None:
        self._definitions: dict[str, VerificationDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: VerificationDefinition) -> None:
        if definition.definition_id in self._definitions:
            raise ValueError(f"duplicate verification definition: {definition.definition_id}")
        self._definitions[definition.definition_id] = definition

    def get(self, definition_id: str) -> VerificationDefinition:
        try:
            return self._definitions[definition_id]
        except KeyError as exc:
            raise KeyError(f"unknown verification definition: {definition_id}") from exc

    def all(self) -> tuple[VerificationDefinition, ...]:
        return tuple(self._definitions.values())


AUTO = frozenset({VerificationMethod.AUTOMATED, VerificationMethod.AUTOMATED_PLUS_HUMAN})
HUMAN = frozenset({VerificationMethod.HUMAN, VerificationMethod.AUTOMATED_PLUS_HUMAN})
EXTERNAL = frozenset({VerificationMethod.EXTERNAL, VerificationMethod.FOUNDER_ONLY})


def _definition(
    definition_id: str,
    description: str,
    tests: tuple[str, ...],
    evidence: tuple[EvidenceType, ...],
    methods: frozenset[VerificationMethod] = AUTO,
    ttl: int | None = 90 * 24 * 60 * 60,
) -> VerificationDefinition:
    return VerificationDefinition(
        definition_id=definition_id,
        description=description,
        allowed_methods=methods,
        defined_tests=frozenset(tests),
        required_evidence_types=frozenset(evidence),
        default_ttl_seconds=ttl,
    )


def default_registry() -> VerificationDefinitionRegistry:
    T = EvidenceType.TEST_RESULT
    return VerificationDefinitionRegistry(
        (
            _definition("website.deployed", "Production deployment answers independently.", ("production-smoke",), (T, EvidenceType.API_RESPONSE)),
            _definition("website.https", "Production TLS is valid for the owned hostname.", ("https-handshake",), (T, EvidenceType.DNS_OBSERVATION)),
            _definition("website.forms", "Valid, invalid and duplicate form paths behave as specified.", ("form-roundtrip",), (T, EvidenceType.API_RESPONSE)),
            _definition("website.mobile", "Supported mobile viewports pass the defined review.", ("mobile-viewport",), (T, EvidenceType.HUMAN_REVIEW), HUMAN),
            _definition("website.links", "Internal and required external links pass the link test.", ("link-crawl",), (T,)),
            _definition("domain.ownership", "Founder/customer controls the domain account.", ("dns-control",), (T, EvidenceType.PROVIDER_RECEIPT, EvidenceType.FOUNDER_ATTESTATION), EXTERNAL, 365 * 24 * 60 * 60),
            _definition("email.inbound", "A test message reaches the customer-owned inbox.", ("inbound-roundtrip",), (T, EvidenceType.PROVIDER_RECEIPT)),
            _definition("email.outbound", "A test message is accepted and delivery evidence recorded.", ("outbound-roundtrip",), (T, EvidenceType.PROVIDER_RECEIPT)),
            _definition("crm.lead_capture", "A lead is stored, routed and retrievable.", ("lead-capture-roundtrip",), (T, EvidenceType.API_RESPONSE)),
            _definition("scheduling.booking", "Booking, conflict and cancellation paths pass.", ("booking-roundtrip", "booking-conflict"), (T, EvidenceType.API_RESPONSE)),
            _definition("payment.test", "Test-mode success and failure paths pass without claiming live authority.", ("payment-test-mode",), (T, EvidenceType.PROVIDER_RECEIPT), EXTERNAL),
            _definition("founder.action_complete", "A founder-only action has acceptable scoped evidence.", ("founder-action-review",), (T, EvidenceType.FOUNDER_ATTESTATION), EXTERNAL),
            _definition("workflow.quote", "Standard and exception quote paths follow approved policy.", ("quote-standard", "quote-exception"), (T, EvidenceType.AUDIT_RECORD)),
            _definition("workflow.rollback", "The intake/booking failure path is documented and tested.", ("rollback-drill",), (T, EvidenceType.AUDIT_RECORD)),
            _definition("monitoring.active", "Ownership and alert routing are active.", ("monitor-alert",), (T, EvidenceType.AUDIT_RECORD)),
            _definition("handoff.complete", "Customer-owned export and access handoff pass review.", ("handoff-export",), (T, EvidenceType.HUMAN_REVIEW), HUMAN),
        )
    )
