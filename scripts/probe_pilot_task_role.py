"""One-shot non-production ECS task-role probe; never prints secret material."""

from __future__ import annotations

import json
import os
import time

import boto3


ACCOUNT = "199949321335"
REGION = "us-east-1"
CLUSTER = "businessbuilder-staging"
SERVICE = "businessbuilder-pilot-auth"
TASK_DEFINITION = "businessbuilder-pilot-auth-v1:14"
LOG_GROUP = "/ecs/businessbuilder-staging"


def probe() -> dict[str, str]:
    expected = os.environ.get("PILOT_ROLE_PROBE_EXPECT", "ALLOW")
    if expected not in {"ALLOW", "DENY"}:
        raise ValueError("pilot role probe expectation must be ALLOW or DENY")
    if boto3.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise ValueError("wrong AWS account")
    ecs = boto3.client("ecs", region_name=REGION)
    service = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    if service["status"] != "ACTIVE" or service["desiredCount"] != 1:
        raise RuntimeError("isolated pilot service shape changed")
    definition = ecs.describe_task_definition(taskDefinition=TASK_DEFINITION)["taskDefinition"]
    if definition["taskRoleArn"] != f"arn:aws:iam::{ACCOUNT}:role/businessbuilder-staging-ecs-task-role":
        raise RuntimeError("pilot task role changed")
    code = (
        "import boto3\n"
        "from businessbuilder.commercial.pilot_access import AwsPilotCodeReader\n"
        "print('ROLE=' + boto3.client('sts').get_caller_identity()['Arn'], flush=True)\n"
        "try:\n"
        "    AwsPilotCodeReader().read_code()\n"
        "    print('SECRET_READ=ALLOW', flush=True)\n"
        "except Exception:\n"
        "    print('SECRET_READ=DENY', flush=True)\n"
    )
    response = ecs.run_task(
        cluster=CLUSTER,
        taskDefinition=TASK_DEFINITION,
        launchType="FARGATE",
        networkConfiguration=service["networkConfiguration"],
        overrides={"containerOverrides": [
            {"name": "backend", "command": ["python", "-c", code]},
            {"name": "site", "command": ["sleep", "30"]},
        ]},
        tags=[{"key": "Purpose", "value": "pilot-secret-role-probe"}],
    )
    if response.get("failures") or len(response.get("tasks", [])) != 1:
        raise RuntimeError("one-shot role probe did not start")
    task_arn = response["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    for _ in range(45):
        task = ecs.describe_tasks(cluster=CLUSTER, tasks=[task_arn])["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            break
        time.sleep(2)
    else:
        raise RuntimeError("one-shot role probe did not stop")
    stream = f"pilot-auth-backend/backend/{task_id}"
    logs = boto3.client("logs", region_name=REGION)
    markers: dict[str, str] = {}
    for _ in range(10):
        try:
            events = logs.get_log_events(logGroupName=LOG_GROUP, logStreamName=stream)["events"]
        except logs.exceptions.ResourceNotFoundException:
            events = []
        for event in events:
            message = event["message"].strip()
            if message.startswith("ROLE="):
                markers["role"] = message[5:]
            elif message in {"SECRET_READ=ALLOW", "SECRET_READ=DENY"}:
                markers["secret_read"] = message.split("=", 1)[1]
        if markers.get("secret_read"):
            break
        time.sleep(1)
    if markers.get("secret_read") != expected or ":assumed-role/businessbuilder-staging-ecs-task-role/" not in markers.get("role", ""):
        raise RuntimeError("one-shot task role proof did not match expectation")
    return {"status": "passed", "task": task_arn, "role": markers["role"],
            "secret_read": markers["secret_read"]}


if __name__ == "__main__":
    try:
        print(json.dumps(probe()), flush=True)
    except Exception as error:
        # Never expose AWS responses or log payloads.
        print(json.dumps({"status": "failed", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
