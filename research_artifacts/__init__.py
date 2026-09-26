"""Research Artifact Layer v0.1.2 with deterministic orchestration."""

from .bootstrap import initialize_workspace
from .workspace import (
    ArtifactError,
    ArtifactWorkspace,
    RevisionConflict,
    ValidationIssue,
)
from .orchestrator import (
    CheckpointConflict,
    CheckpointStore,
    GateEvaluator,
    InvalidEvent,
    OrchestratorEvent,
    OrchestratorValidationError,
    ResearchOrchestrator,
    StateReducer,
)
from .artifact_service import ArtifactService, Proposal
from .action_compiler import ActionCompiler
from .tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from .mvp import MVPController, SyntheticSkillRuntime
from .context_builder import ResearchContextBuilder
from .transactions import ScientificTransaction
from .cost_control import (
    BudgetApprovalGate,
    BudgetGuard,
    BudgetStore,
    CostLedger,
    ModelRouter,
)

__all__ = [
    "initialize_workspace",
    "ArtifactError",
    "ArtifactWorkspace",
    "RevisionConflict",
    "ValidationIssue",
    "CheckpointConflict",
    "CheckpointStore",
    "GateEvaluator",
    "InvalidEvent",
    "OrchestratorEvent",
    "OrchestratorValidationError",
    "ResearchOrchestrator",
    "StateReducer",
    "ArtifactService",
    "ActionCompiler",
    "ExperimentExecutionAdapter",
    "LeanToolAdapter",
    "MVPController",
    "SyntheticSkillRuntime",
    "Proposal",
    "ResearchContextBuilder",
    "ScientificTransaction",
    "BudgetApprovalGate",
    "BudgetGuard",
    "BudgetStore",
    "CostLedger",
    "ModelRouter",
]

__version__ = "0.1.1"
