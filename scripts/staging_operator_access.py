"""Inspect and provision operator access in a non-production staging schema.

This changes nothing about the authorization model. It calls the same
``IdentityService`` the product calls, so every write is owner-authorized,
company-scoped, expiring and audited, and the grant is fixed to the two
read-only permissions the dashboard preview needs. Billing is never included:
``VIEW_BILLING`` sits inside ``SUPPORT_NEVER_ALLOWED`` and cannot be granted to
a support user by any path.

It never prints a password, token, session, cookie, signing key or secret. It
prints internal identifiers, membership state and grant state only.

Refuses to run against a schema that looks like production.

    # Report only. Writes nothing.
    python scripts/staging_operator_access.py inspect --email operator@example.test

    # Give an existing user SUPPORT membership in the owner's tenant.
    python scripts/staging_operator_access.py provision-membership \\
        --owner-email founder@example.test --email operator@example.test

    # Issue a scoped, expiring, read-only grant.
    python scripts/staging_operator_access.py provision-grant \\
        --owner-email founder@example.test --email operator@example.test \\
        --company-id company_pilot_x --minutes 480 --reason "owner review"
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import os
import sys

from businessbuilder.identity import (
    AuthorizationContext,
    MembershipStatus,
    Permission,
    Role,
)
from businessbuilder.postgres import PostgresCompanyBrainRepository, PostgresIdentityRepository
from businessbuilder.company_brain import CompanyBrainService, Scope
from businessbuilder.identity import IdentityService
from businessbuilder.runtime.ids import random_id


# The grant the read-only dashboard preview needs, and nothing beyond it.
PREVIEW_PERMISSIONS = frozenset({Permission.VIEW_COMPANY_STATE, Permission.ACCESS_ARTIFACTS})
# A schema whose name says production is never a valid target for this tool.
PRODUCTION_MARKERS = ("prod", "production", "live")
MAX_MINUTES = 7 * 24 * 60


def now() -> datetime:
    return datetime.now(timezone.utc)


def guard_non_production(schema: str, environment: str) -> None:
    lowered = f"{schema} {environment}".lower()
    if any(marker in lowered for marker in PRODUCTION_MARKERS):
        raise SystemExit(f"refusing to run against a production-looking target: schema={schema}")


def repository(args) -> PostgresIdentityRepository:
    return PostgresIdentityRepository(args.dsn, schema=args.schema)


def find_user(repo, email: str):
    user = repo.get_user_by_email(email.strip().lower())
    if user is None:
        raise SystemExit(f"no internal user exists for that email in schema {repo and ''}".strip())
    return user


def describe_memberships(repo, user_id: str) -> list[dict]:
    rows = []
    for membership in repo.list_user_memberships(user_id):
        try:
            organization = repo.get_organization(membership.organization_id)
        except LookupError:
            continue
        rows.append({
            "membership_id": membership.membership_id,
            "organization": organization.display_name,
            "organization_id": organization.organization_id,
            "role": membership.role.value,
            "status": membership.status.value,
            "companies": list(organization.company_ids),
        })
    return rows


def describe_grants(repo, user_id: str, at: datetime) -> list[dict]:
    return [
        {
            "grant_id": grant.grant_id,
            "company_id": grant.company_id,
            "company_scoped": grant.company_id is not None,
            "permissions": sorted(item.value for item in grant.permissions),
            "reason": grant.reason,
            "expires_at": grant.ends_at.isoformat(),
            "active": grant.active_at(at),
            "billing_included": Permission.VIEW_BILLING in grant.permissions,
        }
        for grant in repo.list_support_grants_for_user(user_id)
    ]


def company_names(args, tenant_id: str, company_ids) -> dict[str, str]:
    names: dict[str, str] = {}
    brain_repo = PostgresCompanyBrainRepository(args.dsn, schema=args.schema)
    try:
        brain = CompanyBrainService(brain_repo)
        for company_id in company_ids:
            try:
                names[company_id] = brain.get_company(Scope(tenant_id, company_id)).display_name
            except Exception:  # noqa: BLE001 - a missing Brain record is reportable, not fatal
                names[company_id] = "(no Company Brain record)"
    finally:
        brain_repo.close()
    return names


def report(title: str, rows) -> None:
    print(f"\n{title}")
    if not rows:
        print("  (none)")
        return
    for row in rows:
        print("  " + " | ".join(f"{key}={value}" for key, value in row.items()))


def command_inspect(args) -> int:
    repo = repository(args)
    try:
        at = now()
        user = repo.get_user_by_email(args.email.strip().lower())
        if user is None:
            print(f"No internal user exists for {args.email} in schema {args.schema}.")
            print("Sign in through Cognito once to create the internal user, then re-run.")
            return 1
        print(f"user_id           : {user.user_id}")
        print(f"email             : {user.email}")
        print(f"status            : {user.status.value}")
        print(f"email_verified    : {user.email_verified_at is not None}")
        print(f"auth_provider     : {user.authentication_provider or '(none recorded)'}")

        memberships = describe_memberships(repo, user.user_id)
        report("Memberships", memberships)

        support = [
            item for item in memberships
            if item["role"] == Role.SUPPORT.value and item["status"] == MembershipStatus.ACTIVE.value
        ]
        print(f"\nSUPPORT authority : {'yes' if support else 'no'}")

        grants = describe_grants(repo, user.user_id, at)
        report("Support grants issued to this user", grants)
        active = [item for item in grants if item["active"]]
        print(f"\nActive grants     : {len(active)}")
        print(f"Preview ready     : {'yes' if support and active else 'no'}")
        if any(item["billing_included"] for item in grants):
            print("WARNING: a grant lists billing. The model forbids it; investigate before use.")

        for item in support:
            names = company_names(args, _tenant_for(repo, item["membership_id"]), item["companies"])
            report(
                f"Companies in {item['organization']}",
                [{"company_id": cid, "display_name": name} for cid, name in names.items()],
            )
        return 0
    finally:
        repo.close()


def _tenant_for(repo, membership_id: str) -> str:
    return repo.get_membership(membership_id).tenant_id


def _owner_context(repo, owner_email: str) -> AuthorizationContext:
    owner = repo.get_user_by_email(owner_email.strip().lower())
    if owner is None:
        raise SystemExit("no internal user exists for the owner email")
    owned = [
        membership for membership in repo.list_user_memberships(owner.user_id)
        if membership.status is MembershipStatus.ACTIVE and membership.role is Role.OWNER
    ]
    if len(owned) != 1:
        raise SystemExit(f"owner must hold exactly one active OWNER membership, found {len(owned)}")
    return AuthorizationContext(owner.user_id, owned[0].tenant_id)


def command_provision_membership(args) -> int:
    repo = repository(args)
    try:
        identity = IdentityService(repo, id_factory=random_id, clock=now)
        context = _owner_context(repo, args.owner_email)
        operator = repo.get_user_by_email(args.email.strip().lower())
        if operator is None:
            raise SystemExit("the operator must sign in through Cognito once before being invited")

        existing = repo.get_active_membership(context.tenant_id, operator.user_id)
        if existing is not None and existing.role is Role.SUPPORT:
            print(f"already active: membership_id={existing.membership_id} role=support")
            return 0
        if existing is not None:
            raise SystemExit(
                f"that user already holds an active {existing.role.value} membership in this tenant; "
                "an operator identity must be separate from the founder identity"
            )

        invited = identity.invite_member(
            context, operator.user_id, Role.SUPPORT, reason=args.reason,
        )
        accepted = identity.accept_membership(invited.membership_id, operator.user_id)
        print(f"membership_id={accepted.membership_id} role={accepted.role.value} status={accepted.status.value}")
        print("Both the invite and the acceptance are recorded in the identity audit trail.")
        return 0
    finally:
        repo.close()


def command_provision_grant(args) -> int:
    if not 1 <= args.minutes <= MAX_MINUTES:
        raise SystemExit(f"minutes must be between 1 and {MAX_MINUTES}")
    repo = repository(args)
    try:
        identity = IdentityService(repo, id_factory=random_id, clock=now)
        context = _owner_context(repo, args.owner_email)
        operator = repo.get_user_by_email(args.email.strip().lower())
        if operator is None:
            raise SystemExit("no internal user exists for the operator email")

        membership = repo.get_active_membership(context.tenant_id, operator.user_id)
        if membership is None or membership.role is not Role.SUPPORT:
            raise SystemExit("the operator needs an active SUPPORT membership first")

        organization = repo.get_organization_by_tenant(context.tenant_id)
        if args.company_id not in organization.company_ids:
            raise SystemExit("that company is outside the owner's organization")

        grant = identity.grant_support_access(
            context,
            operator.user_id,
            PREVIEW_PERMISSIONS,
            timedelta(minutes=args.minutes),
            args.reason,
            company_id=args.company_id,
        )
        print(f"grant_id={grant.grant_id}")
        print(f"company_id={grant.company_id}")
        print(f"permissions={sorted(item.value for item in grant.permissions)}")
        print(f"expires_at={grant.ends_at.isoformat()}")
        print(f"billing_included={Permission.VIEW_BILLING in grant.permissions}")
        print("Recorded as support_access.granted in the identity audit trail.")
        return 0
    finally:
        repo.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--schema", default=os.environ.get("DB_SCHEMA"))
    parser.add_argument("--environment", default=os.environ.get("ENVIRONMENT", "staging"))
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="report operator readiness; writes nothing")
    inspect.add_argument("--email", required=True)
    inspect.set_defaults(handler=command_inspect)

    membership = sub.add_parser("provision-membership", help="owner grants SUPPORT membership")
    membership.add_argument("--owner-email", required=True)
    membership.add_argument("--email", required=True)
    membership.add_argument("--reason", default="non-production operator dashboard preview")
    membership.set_defaults(handler=command_provision_membership)

    grant = sub.add_parser("provision-grant", help="owner issues a scoped, expiring, read-only grant")
    grant.add_argument("--owner-email", required=True)
    grant.add_argument("--email", required=True)
    grant.add_argument("--company-id", required=True)
    grant.add_argument("--minutes", type=int, default=480)
    grant.add_argument("--reason", default="non-production operator dashboard preview")
    grant.set_defaults(handler=command_provision_grant)

    args = parser.parse_args()
    if not args.dsn or not args.schema:
        print("a --dsn/DATABASE_URL and --schema/DB_SCHEMA are required", file=sys.stderr)
        return 2
    guard_non_production(args.schema, args.environment)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
