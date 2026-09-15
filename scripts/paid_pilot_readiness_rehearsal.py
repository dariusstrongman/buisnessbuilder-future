"""Read-only, nine-gate paid-pilot rehearsal. Never reads credential values.

The matrix is evidence inventory, not an approval authority. Authoritative
approvals still require PaidPilotReleaseGate's independent server verifiers.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Mapping

from businessbuilder.commercial.paid_pilot_release import GateKind


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "PAID_PILOT_REHEARSAL_GATE_MATRIX.json"
SOURCE_CHECKPOINT = "7d78d56c704b2ee43b33135164a135068ee3179b"
MATRIX_BASE_CHECKPOINT = "9b7404f28b0add09e2643ff8136eb7c43c76c373"

# Proposed production configuration NAMES only; presence is never approval.
STRIPE_NAMES = (
    "STRIPE_LIVE_SECRET_REF", "STRIPE_LIVE_WEBHOOK_SECRET_REF",
    "STRIPE_LIVE_ACCOUNT_ID", "STRIPE_LIVE_WEBHOOK_ENDPOINT",
    "STRIPE_LIVE_MONITORING_OWNER", "STRIPE_LIVE_ROLLBACK_REF",
)
COGNITO_NAMES = (
    "COGNITO_PRODUCTION_USER_POOL_ID", "COGNITO_PRODUCTION_APP_CLIENT_ID",
    "COGNITO_PRODUCTION_DOMAIN", "COGNITO_PRODUCTION_CALLBACK_URL",
    "COGNITO_PRODUCTION_LOGOUT_URL", "COGNITO_OPERATOR_MFA_POLICY_REF",
    "COGNITO_EMAIL_DOMAIN_READINESS_REF", "COGNITO_DISABLE_RECONCILIATION_REF",
    "COGNITO_ABUSE_MONITORING_OWNER", "BUSINESS_BUILDER_AUTH_COOKIE_SIGNING_KEY",
    "BUSINESS_BUILDER_API_URL",
)


def _cloud_names(service: str, operation: str, query: str, *, max_results: int | None = None) -> list[str]:
    """Only list resource names. Never call get-secret-value or inspect env."""
    command = ["aws", service, operation, "--query", query, "--output", "json"]
    if max_results is not None:
        command.extend(["--max-results", str(max_results)])
    result = subprocess.run(
        command,
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"name-only AWS inventory failed: {service} {operation}")
    parsed = json.loads(result.stdout)
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def cloud_inventory() -> dict[str, object]:
    pools = _cloud_names("cognito-idp", "list-user-pools", "UserPools[].Name", max_results=60)
    secrets = _cloud_names("secretsmanager", "list-secrets", "SecretList[].Name")
    alarms = _cloud_names("cloudwatch", "describe-alarms", "MetricAlarms[].AlarmName")
    return {
        "pilot_cognito_pool_named": "businessbuilder-pilot-auth-v1" in pools,
        "production_cognito_pool_named": any("businessbuilder-production" in name for name in pools),
        "stripe_test_secret_named": "stripetest" in secrets,
        "stripe_live_secret_named": any("stripe" in name.lower() and "live" in name.lower() for name in secrets),
        "businessbuilder_alarm_named": any("businessbuilder" in name.lower() for name in alarms),
    }


def source_rollback_rehearsal() -> dict[str, object]:
    old = subprocess.run(
        ["git", "show", f"{SOURCE_CHECKPOINT}:src/businessbuilder/commercial/repository.py"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    current = (ROOT / "src" / "businessbuilder" / "commercial" / "repository.py").read_text()
    if "GateRecord" in old or "GateRecord" not in current:
        raise RuntimeError("rollback codec inspection is inconsistent")
    return {
        "old_revision_7_excluded": True,
        "pilot_revision_12_excluded_after_gate_rows": True,
        "reason": "earlier code cannot decode newly persisted release gate records",
        "compatible_rollback_image_drill_completed": False,
    }


def rehearse(matrix: Mapping[str, object], *, environment: Mapping[str, str],
             inspect_cloud: bool = False) -> dict[str, object]:
    if matrix.get("source_checkpoint") != MATRIX_BASE_CHECKPOINT:
        raise ValueError("rehearsal matrix source checkpoint changed")
    rows = matrix.get("gates")
    if not isinstance(rows, list) or len(rows) != len(GateKind):
        raise ValueError("exactly nine existing blocking gates required")
    seen: set[str] = set()
    clean: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {kind.value for kind in GateKind}:
            raise ValueError("unknown gate")
        kind = row["kind"]
        if kind in seen or row.get("status") not in {"HOLD", "NOT_READY"}:
            raise ValueError("rehearsal cannot fabricate an approval")
        seen.add(kind)
        owner = row.get("appointed_owner")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("invalid appointed owner")
        if not isinstance(row.get("owner_role"), str) or not re.fullmatch(r"[a-z0-9_]{3,80}", row["owner_role"]):
            raise ValueError("invalid owner role")
        if not isinstance(row.get("reason"), str) or not re.fullmatch(r"[a-z0-9_]{3,80}", row["reason"]):
            raise ValueError("invalid hold reason")
        evidence = row.get("evidence_refs")
        if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
            raise ValueError("invalid evidence references")
        for ref in evidence:
            if (len(ref) > 256 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", ref)
                or re.search(r"sk_(?:test|live)_|whsec_|AKIA[0-9A-Z]{16}", ref)):
                raise ValueError("unsafe evidence reference")
            if ref.startswith("aws-name:") or ref.startswith("ecs-family:"):
                continue
            if not (ROOT / ref).is_file():
                raise ValueError(f"missing local evidence reference: {ref}")
        clean.append({"kind": kind, "status": row["status"],
                      "owner_role": row.get("owner_role"), "appointed_owner": owner,
                      "evidence_refs": evidence, "reason": row.get("reason")})
    if seen != {kind.value for kind in GateKind}:
        raise ValueError("rehearsal omitted required gate")
    result: dict[str, object] = {
        "decision": "HOLD", "live_charge_allowed": False,
        "gates": clean,
        "stripe_missing_environment_names": [name for name in STRIPE_NAMES if name not in environment],
        "cognito_missing_environment_names": [name for name in COGNITO_NAMES if name not in environment],
        "rollback": source_rollback_rehearsal(),
    }
    if inspect_cloud:
        result["cloud_resource_name_inventory"] = cloud_inventory()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", action="store_true", help="read-only AWS resource-name checks")
    args = parser.parse_args()
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    print(json.dumps(rehearse(matrix, environment=os.environ, inspect_cloud=args.cloud), indent=2))


if __name__ == "__main__":
    main()
