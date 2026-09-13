from __future__ import annotations

from .model import Company, EntityRef, KnowledgeClass, LifecycleState, Provenance, RecordKind, Scope, utc_now
from .service import CompanyBrainService


def load_billy_bob(service: CompanyBrainService, tenant_id: str = "tenant_billy") -> Scope:
    scope = Scope(tenant_id, "co_billy_bob_lawn")
    founder = EntityRef("party", "party_billy")
    stamp = utc_now()
    source = Provenance("founder", stamp, founder, source_ref="fixture://billy-bob-intake", notes="Fictional development fixture")
    company = Company(
        scope=scope, display_name="Billy Bob Lawn Care", legal_name=None, archetype="mobile_service",
        jurisdiction={"country": "US", "region": "TX", "locality": "Denton County"},
        owner_refs=(founder,), lifecycle=LifecycleState.ASSEMBLY,
        provenance=(source,), permissions=("founder.approve", "founder.export", "founder.revoke_access"),
    )
    service.create_company(company)
    facts = [
        ("party_billy", RecordKind.PARTY, {"display_name":"Billy Bob","roles":["founder","account_owner"],"data_owner":True}),
        ("goal_launch", RecordKind.GOAL, {"objective":"Accept the first recurring residential lawn customer through a tested path"}),
        ("strategy_route_density", RecordKind.STRATEGY, {"wedge":"route-dense recurring maintenance","exclusions":["chemicals","irrigation","tree work","hardscape"]}),
        ("offer_recurring_lawn", RecordKind.OFFER, {"name":"Recurring lawn maintenance","service_ids":["service_mow_edge_blow"]}),
        ("service_mow_edge_blow", RecordKind.SERVICE, {"name":"Mow, edge and blow","price_statement":"Starting-price hypothesis only"}),
        ("market_denton_12mi", RecordKind.MARKET, {"center":"North Denton County, TX","radius_miles":12}),
        ("policy_no_regulated_work", RecordKind.POLICY, {"rule":"Do not accept chemical, irrigation, tree or hardscape work"}),
        ("asset_mower", RecordKind.ASSET, {"type":"equipment","name":"21-inch mower","ownership":"founder"}),
        ("asset_trimmer", RecordKind.ASSET, {"type":"equipment","name":"trimmer","ownership":"founder"}),
        ("asset_blower", RecordKind.ASSET, {"type":"equipment","name":"blower","ownership":"founder"}),
        ("account_domain", RecordKind.ACCOUNT, {"type":"domain_registrar","ownership":"founder","status":"founder_action_required"}),
        ("risk_weather", RecordKind.RISK, {"name":"weather disruption","response":"capacity buffer and reschedule policy"}),
        ("obligation_local_check", RecordKind.OBLIGATION, {"name":"location-specific license and permit review","status":"unverified"}),
    ]
    for record_id, kind, data in facts:
        service.record_fact(scope, record_id=record_id, kind=kind, data=data, provenance=(source,), owner_ref=founder)
    service.record_estimate(scope, record_id="capacity_weekly", kind=RecordKind.CAPACITY,
                            data={"founder_hours_per_week":24,"travel_buffer_pct":25,"weather_buffer_pct":15,"safe_weekly_jobs_hypothesis":18},
                            confidence=.45, provenance=(source,), owner_ref=founder)
    service.record_decision(scope, decision_id="decision_launch_wedge",
                            data={"question":"Launch recurring mow/edge/blow first?","choice":"approved","exclusions":["chemicals","irrigation","tree work","hardscape"]},
                            provenance=(source,), owner_ref=founder)
    service.append_founder_action(scope, action_id="founder_buy_domain",
                                  data={"action_type":"purchase","title":"Buy the selected domain in Billy Bob's account","state":"required","irreversible":True},
                                  provenance=(source,), owner_ref=founder)
    return scope
