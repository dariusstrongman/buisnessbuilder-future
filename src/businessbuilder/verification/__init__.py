"""Provider-neutral business verification and readiness subsystem."""

from .catalog import VerificationDefinitionRegistry, default_registry
from .events import VerificationEventHandler
from .models import (
    Blocker,
    BlockerSeverity,
    DependencyRef,
    EvidenceRef,
    EvidenceType,
    ExternalRecord,
    ExternalState,
    FounderActionStatus,
    VerificationMethod,
    VerificationRecord,
    VerificationState,
)
from .ports import CompanySnapshot, CompanySnapshotPort, FounderActionSnapshot
from .readiness import (
    EvaluationResult,
    ReadinessEvaluator,
    ReadinessPolicy,
    Requirement,
    RequirementKind,
    billy_bob_policy,
)
from .repository import InMemoryVerificationRepository, JsonVerificationRepository
from .service import (
    IllegalTransitionError,
    MissingEvidenceError,
    VerificationService,
)

__all__ = [
    "Blocker",
    "BlockerSeverity",
    "CompanySnapshot",
    "CompanySnapshotPort",
    "DependencyRef",
    "EvidenceRef",
    "EvidenceType",
    "EvaluationResult",
    "ExternalRecord",
    "ExternalState",
    "FounderActionSnapshot",
    "FounderActionStatus",
    "IllegalTransitionError",
    "InMemoryVerificationRepository",
    "JsonVerificationRepository",
    "MissingEvidenceError",
    "ReadinessEvaluator",
    "ReadinessPolicy",
    "Requirement",
    "RequirementKind",
    "VerificationDefinitionRegistry",
    "VerificationEventHandler",
    "VerificationMethod",
    "VerificationRecord",
    "VerificationService",
    "VerificationState",
    "billy_bob_policy",
    "default_registry",
]
