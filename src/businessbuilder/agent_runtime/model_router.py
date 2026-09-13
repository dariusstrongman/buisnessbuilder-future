from __future__ import annotations

from .models import ModelCandidate, ModelPolicy, ModelSelection
from businessbuilder.runtime.models import Money


class NoEligibleModel(PermissionError):
    pass


class ModelRouter:
    """Ranks only candidates that already meet security and quality policy."""

    def select(
        self,
        candidates: tuple[ModelCandidate, ...],
        policy: ModelPolicy,
        *,
        capability: str,
        remaining_budget: Money,
    ) -> ModelSelection:
        eligible = []
        for candidate in candidates:
            if policy.allowed_providers and candidate.provider not in policy.allowed_providers:
                continue
            if not candidate.available or not candidate.healthy or not candidate.quota_available:
                continue
            if candidate.quality < policy.quality_floor:
                continue
            if capability not in candidate.capabilities:
                continue
            if policy.requires_tools and "tools" not in candidate.capabilities:
                continue
            if policy.requires_vision and "vision" not in candidate.capabilities:
                continue
            if policy.maximum_latency_ms is not None and candidate.latency_ms > policy.maximum_latency_ms:
                continue
            if candidate.estimated_cost.currency != remaining_budget.currency:
                continue
            if candidate.estimated_cost.minor_units > remaining_budget.minor_units:
                continue
            eligible.append(candidate)
        if not eligible:
            raise NoEligibleModel("no model satisfies policy, health, capability, and budget gates")
        selected = min(
            eligible,
            key=lambda item: (item.estimated_cost.minor_units, item.latency_ms, -item.quality, item.provider, item.model),
        )
        return ModelSelection(
            selected.provider,
            selected.model,
            selected.estimated_cost,
            "lowest measured cost among security-, quality-, capability-, health-, quota-, and budget-eligible models",
        )
