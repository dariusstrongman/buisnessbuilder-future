from __future__ import annotations

from datetime import datetime

from .models import (
    BillingMode,
    Entitlement,
    EntitlementClass,
    Feature,
    Package,
    Product,
    ProductCode,
    ProductVersion,
)
from .repository import CommercialRepository


FEATURES = {
    "website.build": Feature("website.build", "Professional website fulfillment", EntitlementClass.STROMATION_MANAGED, False),
    "website.source_export": Feature("website.source_export", "Customer-owned website source and export", EntitlementClass.CUSTOMER_OWNED, True),
    "company.build": Feature("company.build", "Business assembly fulfillment", EntitlementClass.STROMATION_MANAGED, False),
    "company.read_export": Feature("company.read_export", "Company history and account exports", EntitlementClass.CUSTOMER_OWNED, True),
    "artifacts.read_export": Feature("artifacts.read_export", "Customer-owned brand, document, and evidence exports", EntitlementClass.CUSTOMER_OWNED, True),
    "ai_workforce.execute": Feature("ai_workforce.execute", "Bounded AI workforce execution", EntitlementClass.STROMATION_MANAGED, False),
    "inbox.autonomous": Feature("inbox.autonomous", "Autonomous inbox operations", EntitlementClass.STROMATION_MANAGED, False),
    "monitoring.recurring": Feature("monitoring.recurring", "Recurring operational monitoring", EntitlementClass.STROMATION_MANAGED, False),
    "outreach.scheduled": Feature("outreach.scheduled", "Approved scheduled outreach", EntitlementClass.STROMATION_MANAGED, False),
    "optimization.ongoing": Feature("optimization.ongoing", "Ongoing bounded optimization", EntitlementClass.STROMATION_MANAGED, False),
    "support.managed": Feature("support.managed", "Managed operating support", EntitlementClass.STROMATION_MANAGED, False),
}


def _entitlement(feature_code: str) -> Entitlement:
    feature = FEATURES[feature_code]
    return Entitlement(feature_code, feature_code, feature_code, feature.entitlement_class)


PACKAGE_FEATURES: dict[ProductCode, tuple[str, ...]] = {
    ProductCode.BUILD_WEBSITE: (
        "website.build", "website.source_export", "artifacts.read_export",
    ),
    ProductCode.BUILD_BUSINESS: (
        "website.build", "website.source_export", "company.build",
        "company.read_export", "artifacts.read_export",
    ),
    ProductCode.BUILD_AND_RUN: (
        "company.read_export", "artifacts.read_export", "ai_workforce.execute",
        "inbox.autonomous", "monitoring.recurring", "outreach.scheduled",
        "optimization.ongoing", "support.managed",
    ),
}


DISPLAY_NAMES = {
    ProductCode.BUILD_WEBSITE: "Build My Professional Website",
    ProductCode.BUILD_BUSINESS: "Build My Business",
    ProductCode.BUILD_AND_RUN: "Build & Run My Business",
}


def product_version(code: ProductCode, effective_at: datetime) -> ProductVersion:
    feature_codes = PACKAGE_FEATURES[code]
    billing_mode = BillingMode.RECURRING if code is ProductCode.BUILD_AND_RUN else BillingMode.ONE_TIME
    package = Package(code, DISPLAY_NAMES[code], billing_mode, feature_codes, price_ref=None)
    return ProductVersion(
        f"product_version_{code.value.lower()}_v1", code, 1, package,
        tuple(FEATURES[item] for item in feature_codes),
        tuple(_entitlement(item) for item in feature_codes),
        effective_at,
    )


def seed_default_catalog(repository: CommercialRepository, *, effective_at: datetime) -> tuple[ProductVersion, ...]:
    versions = tuple(product_version(code, effective_at) for code in ProductCode)
    for version in versions:
        repository.save_product(
            Product(version.product_code, version.package.display_name, version.product_version_id),
            version,
        )
    return versions
