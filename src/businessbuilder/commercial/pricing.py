"""The authoritative founding commercial catalog (USD minor units).

Third-party costs and tax are deliberately outside these Business Builder fees.
An existing-business onboarding amount is a floor, never a chargeable price.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OfferCode(StrEnum):
    WEBSITE = "website_build_v1"
    BUSINESS = "new_business_build_v1"
    BUSINESS_RUN = "new_business_build_run_v1"
    EXISTING_RUN = "existing_business_run_v1"


class PriceKind(StrEnum):
    FIXED = "fixed"
    QUOTE_REQUIRED = "quote_required"


@dataclass(frozen=True, slots=True)
class OfferPrice:
    code: OfferCode
    label: str
    kind: PriceKind
    upfront_minor: int | None
    monthly_minor: int | None
    floor_minor: int | None = None
    currency: str = "USD"


FOUNDING_PRICES: dict[OfferCode, OfferPrice] = {
    OfferCode.WEBSITE: OfferPrice(OfferCode.WEBSITE, "Build My Website", PriceKind.FIXED, 79500, None),
    OfferCode.BUSINESS: OfferPrice(OfferCode.BUSINESS, "Build My Business", PriceKind.FIXED, 149500, None),
    OfferCode.BUSINESS_RUN: OfferPrice(OfferCode.BUSINESS_RUN, "Build My Business + Run", PriceKind.FIXED, 199500, 29900),
    OfferCode.EXISTING_RUN: OfferPrice(OfferCode.EXISTING_RUN, "Existing Business + Run", PriceKind.QUOTE_REQUIRED, None, 29900, floor_minor=149500),
}


def public_pricing() -> list[dict[str, object]]:
    return [
        {
            "offer_code": item.code.value,
            "name": item.label,
            "price_kind": item.kind.value,
            "currency": item.currency,
            "upfront_minor": item.upfront_minor,
            "monthly_minor": item.monthly_minor,
            "starting_at_minor": item.floor_minor,
            "third_party_costs_separate": True,
        }
        for item in FOUNDING_PRICES.values()
    ]
