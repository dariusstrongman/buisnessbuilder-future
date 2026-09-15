"""Read-only exact-code scan of recent pilot CloudWatch logs, with no log output."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import boto3


def scan() -> dict[str, int | str]:
    client = boto3.client("secretsmanager", region_name="us-east-1")
    configured = json.loads(client.get_secret_value(SecretId="business-builder/pilot-access")["SecretString"])["promo_code"]
    if not isinstance(configured, str) or len(configured) < 8:
        raise RuntimeError("pilot log scan input unavailable")
    logs = boto3.client("logs", region_name="us-east-1")
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    paginator = logs.get_paginator("filter_log_events")
    checked = matches = pages = 0
    for page in paginator.paginate(
        logGroupName="/ecs/businessbuilder-staging",
        startTime=int(since.timestamp() * 1000),
        PaginationConfig={"PageSize": 1000},
    ):
        pages += 1
        if pages > 100:
            raise RuntimeError("pilot log scan pagination incomplete")
        for event in page.get("events", []):
            checked += 1
            matches += configured in event.get("message", "")
    return {"status": "PASS" if matches == 0 else "FAIL", "events_checked": checked,
            "matches": matches}


if __name__ == "__main__":
    try:
        result = scan()
        print(json.dumps(result), flush=True)
        if result["matches"]:
            raise SystemExit(1)
    except Exception as error:
        # Never print a secret or CloudWatch event, including on SDK failure.
        print(json.dumps({"status": "INCOMPLETE", "failure_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
