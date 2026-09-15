"""Reversible task-role denial rehearsal; leaves the pilot secret unchanged."""

from __future__ import annotations

import json
import os
import time

import boto3

from probe_pilot_task_role import ACCOUNT, probe


ROLE = "businessbuilder-staging-ecs-task-role"
BASE_POLICY = "BusinessBuilderPilotAccessRead"
DENY_POLICY = "BusinessBuilderPilotAccessRehearsalDeny"


def decision(iam, resource: str) -> str:
    result = iam.simulate_principal_policy(
        PolicySourceArn=f"arn:aws:iam::{ACCOUNT}:role/{ROLE}",
        ActionNames=["secretsmanager:GetSecretValue"], ResourceArns=[resource],
    )
    return result["EvaluationResults"][0]["EvalDecision"]


def rehearse() -> dict[str, str]:
    iam = boto3.client("iam")
    document = iam.get_role_policy(RoleName=ROLE, PolicyName=BASE_POLICY)["PolicyDocument"]
    statement = document["Statement"]
    if len(statement) != 1 or statement[0]["Effect"] != "Allow" or (
        statement[0]["Action"] != ["secretsmanager:GetSecretValue"]
        or len(statement[0]["Resource"]) != 1
        or ":secret:business-builder/pilot-access-" not in statement[0]["Resource"][0]
    ):
        raise RuntimeError("base pilot IAM policy shape changed")
    resource = statement[0]["Resource"][0]
    try:
        iam.get_role_policy(RoleName=ROLE, PolicyName=DENY_POLICY)
    except iam.exceptions.NoSuchEntityException:
        pass
    else:
        raise RuntimeError("temporary deny policy unexpectedly exists")
    if decision(iam, resource) != "allowed":
        raise RuntimeError("pilot read was not initially allowed")
    engaged = False
    restored = False
    result: dict[str, str] = {}
    try:
        iam.put_role_policy(
            RoleName=ROLE, PolicyName=DENY_POLICY,
            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                "Effect": "Deny", "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [resource],
            }]}),
        )
        engaged = True
        for _ in range(10):
            if decision(iam, resource) == "explicitDeny":
                break
            time.sleep(1)
        else:
            raise RuntimeError("pilot role deny did not appear in IAM simulator")
        time.sleep(20)
        os.environ["PILOT_ROLE_PROBE_EXPECT"] = "DENY"
        result = probe()
    finally:
        if engaged:
            for _ in range(3):
                try:
                    iam.delete_role_policy(RoleName=ROLE, PolicyName=DENY_POLICY)
                    restored = True
                    break
                except Exception:
                    time.sleep(1)
            if not restored:
                raise RuntimeError("CRITICAL: temporary pilot secret deny was not removed")
            for _ in range(10):
                if decision(iam, resource) == "allowed":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("CRITICAL: pilot role access not restored")
    return {"status": "passed", "secret_read_while_denied": result["secret_read"],
            "policy_restored": "true", "task": result["task"]}


if __name__ == "__main__":
    try:
        print(json.dumps(rehearse()), flush=True)
    except Exception as error:
        # No secret values or AWS response payloads appear in output.
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
