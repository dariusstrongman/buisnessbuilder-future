from __future__ import annotations

import json
import os
import secrets

from .bootstrap import create_postgres_agent_runtime
from .queue import SqsQueue


def main() -> int:
    environment = os.environ.get("ENVIRONMENT", "").lower()
    if environment not in {"staging", "test", "development", "local"}:
        raise RuntimeError("deterministic agent worker cannot run in production")
    queue_url = os.environ.get("JOB_QUEUE_URL", "")
    if not queue_url:
        raise RuntimeError("JOB_QUEUE_URL is required")
    key = os.environ.get("CUSTOMER_API_PRINCIPAL_KEY", "").encode()
    if len(key) < 32:
        key = secrets.token_bytes(32)
    worker_id = os.environ.get("AGENT_WORKER_ID", "shared-agent-worker")
    app = create_postgres_agent_runtime(
        queue=SqsQueue(queue_url), signing_key=key,
        worker_id=worker_id, dispatcher_id=f"{worker_id}-dispatcher",
    )
    try:
        outcome = app.worker.process_one(wait_seconds=20)
        print(json.dumps({"worker": "shared", "outcome": outcome}), flush=True)
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
