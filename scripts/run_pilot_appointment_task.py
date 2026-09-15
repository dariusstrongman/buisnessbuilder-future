"""Launch one bounded Fargate task to appoint the real Cognito pilot operator.

No standing service, IAM identity, access key, or secret value is created.
"""

from __future__ import annotations

import copy
import json
import os

import boto3

from deploy_unified_pilot_task import ACCOUNT, CLUSTER, REGION, SERVICE


def run() -> str:
    if os.environ.get("ENVIRONMENT") != "pilot":
        raise RuntimeError("appointment task is pilot-only")
    if boto3.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("wrong AWS account")
    ecs = boto3.client("ecs", region_name=REGION)
    service = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    if service.get("status") != "ACTIVE" or service.get("runningCount") != 1:
        raise RuntimeError("healthy pilot service required before appointment")
    definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
    backend = next((item for item in definition["containerDefinitions"] if item["name"] == "backend"), None)
    if backend is None or "@sha256:" not in backend["image"]:
        raise RuntimeError("immutable pilot backend image unavailable")
    backend = copy.deepcopy(backend)
    backend["command"] = ["python", "-m", "businessbuilder.commercial.pilot_appointment"]
    backend.pop("healthCheck", None)
    backend.pop("portMappings", None)
    backend["logConfiguration"]["options"]["awslogs-stream-prefix"] = "pilot-commercial-appointment"
    for name in ("PILOT_FOUNDER_USER_ID", "PILOT_OPERATOR_USER_ID", "PILOT_COMPANY_ID", "PILOT_COMMERCIAL_GRANT_ID"):
        value = os.environ[name]
        if not value or len(value) > 128 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in value):
            raise RuntimeError("pilot appointment locator invalid")
        backend["environment"] = [item for item in backend.get("environment", []) if item["name"] != name]
        backend["environment"].append({"name": name, "value": value})
    request = {
        "family": "businessbuilder-pilot-commercial-appointment-v1",
        "taskRoleArn": definition["taskRoleArn"],
        "executionRoleArn": definition["executionRoleArn"],
        "networkMode": definition["networkMode"],
        "containerDefinitions": [backend],
        "requiresCompatibilities": definition["requiresCompatibilities"],
        "cpu": definition["cpu"], "memory": definition["memory"],
        "runtimePlatform": definition["runtimePlatform"],
        "tags": [
            {"key": "Application", "value": "businessbuilder"},
            {"key": "Environment", "value": "pilot"},
            {"key": "Purpose", "value": "bounded-commercial-operator-appointment-v1"},
        ],
    }
    task_definition = ecs.register_task_definition(**request)["taskDefinition"]["taskDefinitionArn"]
    result = ecs.run_task(
        cluster=CLUSTER, taskDefinition=task_definition, count=1,
        launchType="FARGATE", networkConfiguration=service["networkConfiguration"],
        tags=request["tags"],
    )
    if result.get("failures") or len(result.get("tasks", [])) != 1:
        raise RuntimeError("bounded appointment task could not launch")
    return result["tasks"][0]["taskArn"]


if __name__ == "__main__":
    try:
        print(json.dumps({"status": "appointment_task_started", "task_arn": run()}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
