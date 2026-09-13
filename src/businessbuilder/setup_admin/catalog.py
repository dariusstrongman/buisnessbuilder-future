from __future__ import annotations

from .models import AuthorityGate, Criticality, RequirementDefinition


class RequirementCatalog:
    def __init__(self, definitions: tuple[RequirementDefinition, ...] = ()) -> None:
        self._definitions: dict[str, RequirementDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: RequirementDefinition) -> None:
        if definition.requirement_id in self._definitions:
            raise ValueError(f"duplicate requirement: {definition.requirement_id}")
        self._definitions[definition.requirement_id] = definition

    def get(self, requirement_id: str) -> RequirementDefinition:
        try:
            return self._definitions[requirement_id]
        except KeyError as exc:
            raise KeyError(f"unknown setup/admin requirement: {requirement_id}") from exc

    def for_company(self, archetype: str, *, locality: str | None) -> tuple[RequirementDefinition, ...]:
        return tuple(
            item
            for item in self._definitions.values()
            if archetype in item.archetypes and (not item.locality_required or bool(locality))
        )


MOBILE = frozenset({"mobile_service"})


def default_catalog() -> RequirementCatalog:
    """Product requirements, not legal/tax/insurance determinations or advice."""
    return RequirementCatalog(
        (
            RequirementDefinition(
                "admin.domain_account",
                "Founder-owned domain account",
                "The founder creates and controls the registrar account and purchase.",
                MOBILE,
                frozenset({"ownership", "account"}),
                AuthorityGate.FOUNDER,
                Criticality.MATERIAL,
                founder_action_type="purchase",
                required_evidence_kinds=("provider_receipt", "founder_attestation"),
                blocks=("fully_set",),
                skip_consequence="No founder-owned domain can be verified; Fully Set remains blocked.",
                blocks_fully_set_when_skipped=True,
            ),
            RequirementDefinition(
                "admin.business_formation",
                "Business formation path",
                "Prepare a neutral checklist; only the founder or authorized authority may sign, attest, pay, or file.",
                MOBILE,
                frozenset({"formation"}),
                AuthorityGate.FOUNDER_AND_EXTERNAL,
                Criticality.MATERIAL,
                founder_action_type="file",
                required_evidence_kinds=("founder_attestation", "authority_confirmation"),
                blocks=("fully_set",),
                skip_consequence="Formation remains explicitly unresolved and Fully Set remains blocked.",
                blocks_fully_set_when_skipped=True,
                locality_required=True,
            ),
            RequirementDefinition(
                "admin.insurance_review",
                "Founder insurance review",
                "Present questions and referral options; the founder selects coverage with a qualified provider.",
                MOBILE,
                frozenset({"insurance"}),
                AuthorityGate.FOUNDER_AND_EXTERNAL,
                Criticality.MATERIAL,
                founder_action_type="choose_insurance",
                required_evidence_kinds=("provider_receipt", "founder_confirmation"),
                blocks=("fully_set",),
                skip_consequence="Coverage is unresolved; do not claim insured status and Fully Set remains blocked.",
                blocks_fully_set_when_skipped=True,
            ),
            RequirementDefinition(
                "admin.service_scope_attestation",
                "Service scope attestation",
                "Founder confirms the supported mow, edge, and blow scope and excluded regulated work.",
                MOBILE,
                frozenset({"scope"}),
                AuthorityGate.FOUNDER,
                Criticality.CRITICAL,
                founder_action_type="attest_license",
                required_evidence_kinds=("founder_attestation",),
                blocks=("ready", "fully_set"),
                skip_consequence="Public launch is blocked because the supported service scope is unconfirmed.",
                blocks_fully_set_when_skipped=True,
            ),
            RequirementDefinition(
                "admin.account_recovery",
                "Account recovery map",
                "Record customer-owned recovery paths without storing credentials.",
                MOBILE,
                frozenset({"handoff", "account"}),
                AuthorityGate.FOUNDER,
                Criticality.MATERIAL,
                founder_action_type="connect_account",
                required_evidence_kinds=("founder_attestation",),
                dependency_ids=("admin.domain_account",),
                blocks=("fully_set",),
                skip_consequence="Account handoff and recovery remain incomplete; Fully Set remains blocked.",
                blocks_fully_set_when_skipped=True,
            ),
        )
    )
