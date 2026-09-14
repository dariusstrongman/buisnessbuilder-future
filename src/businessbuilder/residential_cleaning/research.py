from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


SUPPORTED_JURISDICTION = {
    "country": "US",
    "region": "TX",
    "locality": "Denton",
}


def research_packet(*, captured_at: datetime) -> dict[str, Any]:
    """Return the bounded, cited research packet for the one supported pilot.

    This is deliberately not a general research engine. Facts are dated and
    recommendations remain hypotheses until the founder approves them.
    """
    captured = captured_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "packet_version": "residential-cleaning-denton.v1",
        "captured_at": captured,
        "jurisdiction": dict(SUPPORTED_JURISDICTION),
        "status": "bounded_prelaunch_research",
        "limitations": [
            "This packet is not legal, tax, insurance, or financial advice.",
            "Official requirements and fees must be confirmed with the named authority before launch.",
            "No competitor-by-competitor price survey has been completed; proposed pricing is a founder-approved operating hypothesis.",
            "Employment and population data are context signals, not proof of customer demand or revenue.",
        ],
        "sources": [
            {
                "source_id": "census_denton_quickfacts",
                "publisher": "United States Census Bureau",
                "title": "QuickFacts: Denton city, Texas",
                "url": "https://www.census.gov/quickfacts/fact/table/dentoncitytexas/LFE046224",
                "observed_at": captured,
            },
            {
                "source_id": "bls_dfw_oews_2024",
                "publisher": "United States Bureau of Labor Statistics",
                "title": "Occupational Employment and Wages in Dallas-Fort Worth-Arlington — May 2024",
                "url": "https://www.bls.gov/regions/southwest/news-release/2025/occupationalemploymentandwages_dallasfortworth_20250617.htm",
                "observed_at": captured,
            },
            {
                "source_id": "texas_business_permits",
                "publisher": "Office of the Governor of Texas",
                "title": "Business Permit Office",
                "url": "https://gov.texas.gov/business/page/business-permits-office",
                "observed_at": captured,
            },
            {
                "source_id": "denton_permits",
                "publisher": "City of Denton",
                "title": "Permits & Licenses",
                "url": "https://www.cityofdenton.com/237/Permits-Licenses",
                "observed_at": captured,
            },
            {
                "source_id": "sba_launch",
                "publisher": "United States Small Business Administration",
                "title": "Launch your business",
                "url": "https://www.sba.gov/counseling/launch-your-business/",
                "observed_at": captured,
            },
            {
                "source_id": "irs_ein",
                "publisher": "Internal Revenue Service",
                "title": "Get an employer identification number",
                "url": "https://www.irs.gov/businesses/small-businesses-self-employed/get-an-employer-identification-number",
                "observed_at": captured,
            },
        ],
        "findings": [
            {
                "finding_id": "market_context",
                "classification": "fact",
                "statement": "Denton is a growing city with a material residential base; this supports testing a geographically narrow household-service offer but does not prove demand.",
                "source_ids": ["census_denton_quickfacts"],
            },
            {
                "finding_id": "labor_context",
                "classification": "fact",
                "statement": "BLS publishes regional employment and wage context for building and grounds cleaning occupations; the data is a cost-planning input, not a customer price list.",
                "source_ids": ["bls_dfw_oews_2024"],
            },
            {
                "finding_id": "permit_confirmation",
                "classification": "fact",
                "statement": "Texas and Denton direct founders to activity- and locality-specific permit resources; the exact cleaning scope and operating location still require authority confirmation.",
                "source_ids": ["texas_business_permits", "denton_permits", "sba_launch"],
            },
            {
                "finding_id": "administrative_sequence",
                "classification": "fact",
                "statement": "The founder must choose a structure and complete any required registration before completing dependent tax-ID, bank, insurance, and provider steps.",
                "source_ids": ["sba_launch", "irs_ein"],
            },
        ],
        "open_research": [
            {
                "task": "Complete a dated local competitor and publicly visible offer survey before publishing prices.",
                "responsibility": "BUSINESS_BUILDER",
            },
            {
                "task": "Confirm city, county, state, property-use, and service-specific requirements with the relevant authorities.",
                "responsibility": "EXTERNAL_PROVIDER/AUTHORITY",
            },
        ],
    }


def recommendation(*, intake: dict[str, Any], research_record_id: str) -> dict[str, Any]:
    radius = intake["service_radius_miles"]
    return {
        "recommendation_version": "residential-cleaning-denton.v1",
        "vertical": "residential_cleaning",
        "state": "awaiting_founder_approval",
        "research_record_id": research_record_id,
        "target_customer": {
            "description": "Households within the approved Denton service radius seeking recurring, checklist-based residential cleaning.",
            "responsibility": "BUSINESS_BUILDER",
        },
        "service_area": {
            "center": "Denton, Texas",
            "radius_miles": radius,
            "outside_area_behavior": "Do not quote automatically; route to founder review.",
            "responsibility": "FOUNDER_ACTION",
        },
        "offers": [
            {
                "offer_id": "recurring_maintenance_clean",
                "name": "Recurring maintenance clean",
                "cadence": ["weekly", "every_two_weeks"],
                "responsibility": "BUSINESS_BUILDER",
            },
            {
                "offer_id": "initial_deep_clean",
                "name": "Initial or deep clean",
                "quote_rule": "Manual scope confirmation required before price or availability is promised.",
                "responsibility": "FOUNDER_ACTION",
            },
        ],
        "excluded_at_launch": [
            "hazardous-material remediation",
            "biohazard cleaning",
            "mold remediation",
            "pest-control treatment",
            "commercial janitorial contracts",
            "unattended key custody without an approved access policy",
        ],
        "starting_price_logic": {
            "formula": "estimated labor hours × founder-approved target hourly revenue + supplies + travel/condition adjustment",
            "rules": [
                "Do not publish a price until labor assumptions and a minimum job value are approved.",
                "Use a bounded intake for home size, bathrooms, condition, pets, frequency, and add-ons.",
                "Initial/deep cleans and incomplete intake always require founder review.",
                "No agent may invent discounts, taxes, availability, or guarantees.",
            ],
            "responsibility": "FOUNDER_ACTION",
        },
        "positioning": {
            "statement": "A narrow, owner-approved residential cleaning service built around a consistent checklist, reliable communication, and a bounded Denton service area.",
            "prohibited_unverified_claims": ["licensed", "bonded", "insured", "eco-friendly", "background-checked", "guaranteed"],
            "responsibility": "BUSINESS_BUILDER",
        },
        "risks": [
            "Home condition and requested scope can exceed intake assumptions.",
            "Property access, key custody, pets, chemicals, breakage, and worker safety require explicit policies.",
            "Insurance and locality-specific requirements must be externally confirmed.",
            "Travel and rescheduling can erode capacity and margin.",
            "Public prices or claims can become misleading when source facts change.",
        ],
        "startup_admin_requirements": [
            {"requirement": "entity/admin path", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"requirement": "EIN/tax-ID determination and application", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"requirement": "founder-owned business bank account", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"requirement": "insurance selection and verification", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"requirement": "license/permit determination", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
        ],
        "recommended_systems": [
            {"system": "customer website and lead form", "responsibility": "BUSINESS_BUILDER"},
            {"system": "founder-owned business email", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"system": "CRM with new/contacted/quoted/booked/lost stages", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"system": "calendar and booking provider", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"system": "merchant payment and payout provider", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
            {"system": "bookkeeping/accounting account", "responsibility": "FOUNDER_ACTION", "authority": "EXTERNAL_PROVIDER/AUTHORITY"},
        ],
        "approval_effect": "Approval commits this bounded scope to Company Brain and permits creation of the Build My Business order. It does not complete any Founder Action or make the company Ready.",
    }
