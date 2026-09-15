"""One-shot, AWS-deployer-controlled appointment for the isolated pilot only.

The task must run within the existing pilot network. It never creates a
founder, operator, membership, company, or customer authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os

from businessbuilder.commercial.operator_authority import CommercialOperatorAuthority, OPERATOR_ACTIONS
from businessbuilder.identity.models import MembershipStatus, Role
from businessbuilder.postgres import PostgresCommercialRepository, PostgresIdentityRepository


def provision_pilot_appointment() -> str:
    if os.environ.get("ENVIRONMENT") != "pilot":
        raise RuntimeError("pilot appointment cannot run outside the isolated pilot")
    founder_id = os.environ["PILOT_FOUNDER_USER_ID"]
    operator_id = os.environ["PILOT_OPERATOR_USER_ID"]
    company_id = os.environ["PILOT_COMPANY_ID"]
    grant_id = os.environ["PILOT_COMMERCIAL_GRANT_ID"]
    signing_key = os.environ["CUSTOMER_API_PRINCIPAL_KEY"].encode()
    if len(signing_key) < 32 or founder_id == operator_id:
        raise RuntimeError("pilot identities and signing configuration invalid")
    identity = PostgresIdentityRepository()
    commercial = PostgresCommercialRepository()
    try:
        owner_scopes = []
        for membership in identity.list_user_memberships(founder_id):
            if membership.status is not MembershipStatus.ACTIVE or membership.role is not Role.OWNER:
                continue
            organization = identity.get_organization_by_tenant(membership.tenant_id)
            if company_id in organization.company_ids:
                owner_scopes.append(membership.tenant_id)
        if len(owner_scopes) != 1:
            raise RuntimeError("founder OWNER company scope unavailable")
        tenant_id = owner_scopes[0]
        support = identity.get_active_membership(tenant_id, operator_id)
        if support is None or support.role is not Role.SUPPORT:
            raise RuntimeError("current operator SUPPORT membership unavailable")
        marker = object()
        authority = CommercialOperatorAuthority(
            identity, commercial,
            signing_key=sha256(b"commercial-operator-v1:" + signing_key).digest(),
            clock=lambda: datetime.now(timezone.utc),
            privileged_provisioner_verifier=lambda context: "pilot_aws_deployer"
            if context is marker else "",
        )
        try:
            existing = commercial.get_operator_grant(tenant_id, company_id, grant_id)
        except LookupError:
            existing = None
        if existing is not None:
            if (existing.operator_user_id != operator_id or existing.actions != OPERATOR_ACTIONS
                or not existing.active_at(datetime.now(timezone.utc))):
                raise RuntimeError("existing pilot appointment conflicts or expired")
            return grant_id
        now = datetime.now(timezone.utc)
        authority.provision(
            marker, grant_id=grant_id, operator_user_id=operator_id,
            tenant_id=tenant_id, company_id=company_id,
            actions=OPERATOR_ACTIONS, starts_at=now,
            ends_at=now + timedelta(hours=8),
            reason_code="unified_cognito_stripe_sandbox_acceptance",
        )
        return grant_id
    finally:
        identity.close()
        commercial.close()


if __name__ == "__main__":
    try:
        print(json.dumps({"status": "appointed", "grant_id": provision_pilot_appointment()}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
