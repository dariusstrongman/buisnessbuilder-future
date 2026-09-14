from .capabilities import DeterministicAgentCapability, SAFE_AGENT_ACTIONS
from .model_router import ModelRouter, NoEligibleModel
from .models import (
    AgentJobEnvelope,
    DeliveryState,
    ExecutionRecord,
    ExecutionState,
    ModelCandidate,
    ModelPolicy,
    ModelSelection,
    QueueDelivery,
    QueueOutboxRecord,
    RuntimeSchedule,
    TriggerClass,
)
from .queue import InMemoryQueue, QueuePort, SqsQueue
from .scheduler import EventBridgeSchedulerAdapter, RuntimeScheduler
from .service import (
    AgentAdmissionDenied,
    AgentOutboxDispatcher,
    AgentRuntimeService,
    EntitlementGate,
    SharedAgentWorker,
    SimulatedWorkerCrash,
)

__all__ = [
    "AgentAdmissionDenied", "AgentJobEnvelope", "AgentOutboxDispatcher",
    "AgentRuntimeService", "DeliveryState", "DeterministicAgentCapability",
    "EntitlementGate", "ExecutionRecord", "ExecutionState", "InMemoryQueue",
    "ModelCandidate", "ModelPolicy", "ModelRouter", "ModelSelection",
    "NoEligibleModel", "QueueDelivery", "QueueOutboxRecord", "QueuePort",
    "RuntimeSchedule", "SAFE_AGENT_ACTIONS", "SharedAgentWorker",
    "SimulatedWorkerCrash", "SqsQueue", "TriggerClass",
    "EventBridgeSchedulerAdapter", "RuntimeScheduler",
    "PostgresAgentRuntime", "create_postgres_agent_runtime",
]


def __getattr__(name: str):
    if name in {"PostgresAgentRuntime", "create_postgres_agent_runtime"}:
        from .bootstrap import PostgresAgentRuntime, create_postgres_agent_runtime
        return {
            "PostgresAgentRuntime": PostgresAgentRuntime,
            "create_postgres_agent_runtime": create_postgres_agent_runtime,
        }[name]
    raise AttributeError(name)
