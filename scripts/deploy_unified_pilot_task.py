"""Update only the existing non-production Cognito pilot ECS service.

Image digests and secret ARNs are locators. No secret value is read or printed.
The previous healthy task definition remains an explicit rollback target.
"""

from __future__ import annotations

import copy
import json
import os
import re

import boto3


REGION = "us-east-1"
ACCOUNT = "199949321335"
CLUSTER = "businessbuilder-staging"
SERVICE = "businessbuilder-pilot-auth"
FAMILY = "businessbuilder-pilot-auth-v1"
ECR_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/businessbuilder-staging-app"
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _environment(container: dict, name: str, value: str) -> None:
    container["environment"] = [item for item in container.get("environment", []) if item["name"] != name]
    container["environment"].append({"name": name, "value": value})


def _secret(container: dict, name: str, arn: str, field: str) -> None:
    container["secrets"] = [item for item in container.get("secrets", []) if item["name"] != name]
    container["secrets"].append({"name": name, "valueFrom": f"{arn}:{field}::"})


def deploy() -> tuple[str, str]:
    if os.environ.get("ENVIRONMENT") != "pilot":
        raise RuntimeError("unified deployment is limited to pilot mode")
    backend_digest = os.environ["PILOT_BACKEND_IMAGE_DIGEST"]
    site_digest = os.environ["PILOT_SITE_IMAGE_DIGEST"]
    if not IMAGE_DIGEST.fullmatch(backend_digest) or not IMAGE_DIGEST.fullmatch(site_digest):
        raise RuntimeError("immutable pilot image digests required")
    stripe_key_ref = os.environ["PILOT_STRIPE_TEST_SECRET_ARN"]
    stripe_signer_ref = os.environ["PILOT_STRIPE_WEBHOOK_SECRET_ARN"]
    if (not stripe_key_ref.startswith(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:stripetest-")
        or not stripe_signer_ref.startswith(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:businessbuilder-pilot-stripe-webhook-v1-")):
        raise RuntimeError("only exact isolated Stripe sandbox secret families are allowed")
    if boto3.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise RuntimeError("wrong AWS account")
    ecs = boto3.client("ecs", region_name=REGION)
    service = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    if (service.get("status") != "ACTIVE" or service.get("desiredCount") != 1
        or not service.get("taskDefinition", "").split("/")[-1].startswith(FAMILY + ":")):
        raise RuntimeError("existing isolated pilot service changed unexpectedly")
    running_arns = ecs.list_tasks(cluster=CLUSTER, serviceName=SERVICE,
                                  desiredStatus="RUNNING").get("taskArns", [])
    running = ecs.describe_tasks(cluster=CLUSTER, tasks=running_arns).get("tasks", []) if running_arns else []
    healthy = [task for task in running if task.get("lastStatus") == "RUNNING"
               and all(container.get("healthStatus") == "HEALTHY"
                       for container in task.get("containers", []))]
    if len(healthy) != 1:
        raise RuntimeError("one healthy rollback task required")
    original_arn = healthy[0]["taskDefinitionArn"]
    original = ecs.describe_task_definition(taskDefinition=original_arn, include=["TAGS"])
    definition = original["taskDefinition"]
    if definition["family"] != FAMILY or len(definition["containerDefinitions"]) != 2:
        raise RuntimeError("pilot task definition shape changed")
    permitted = (
        "family", "taskRoleArn", "executionRoleArn", "networkMode",
        "containerDefinitions", "volumes", "placementConstraints",
        "requiresCompatibilities", "cpu", "memory", "runtimePlatform",
        "ephemeralStorage", "proxyConfiguration", "ipcMode", "pidMode",
    )
    request = {name: copy.deepcopy(definition[name]) for name in permitted if name in definition}
    containers = {item["name"]: item for item in request["containerDefinitions"]}
    if set(containers) != {"backend", "site"}:
        raise RuntimeError("pilot containers changed unexpectedly")
    backend, site = containers["backend"], containers["site"]
    backend["image"] = f"{ECR_REPOSITORY}@{backend_digest}"
    site["image"] = f"{ECR_REPOSITORY}@{site_digest}"
    _environment(backend, "APP_VERSION", "unified-cognito-stripe-sandbox-v1")
    _environment(backend, "PILOT_SUPERVISED_STRIPE_TEST", "1")
    _environment(backend, "BUSINESS_BUILDER_CHECKOUT_SUCCESS_URL",
                 "https://d3qncwxo58gn5b.cloudfront.net/build-room?checkout=returned")
    _environment(backend, "BUSINESS_BUILDER_CHECKOUT_CANCEL_URL",
                 "https://d3qncwxo58gn5b.cloudfront.net/build-room?checkout=canceled")
    _secret(backend, "STRIPE_TEST_SECRET_KEY", stripe_key_ref, "STRIPE_TEST_SECRET_KEY")
    _secret(backend, "STRIPE_TEST_WEBHOOK_SECRET", stripe_signer_ref, "STRIPE_TEST_WEBHOOK_SECRET")
    tags = [item for item in original.get("tags", []) if item["key"] != "Purpose"]
    tags.append({"key": "Purpose", "value": "unified-cognito-stripe-sandbox-acceptance-v1"})
    new_arn = ecs.register_task_definition(**request, tags=tags)["taskDefinition"]["taskDefinitionArn"]
    ecs.update_service(cluster=CLUSTER, service=SERVICE, taskDefinition=new_arn)
    return original_arn, new_arn


if __name__ == "__main__":
    try:
        previous, current = deploy()
        print(json.dumps({"status": "pilot_update_started", "previous_task": previous,
                          "new_task": current}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
