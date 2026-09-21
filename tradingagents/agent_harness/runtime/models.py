"""Typed contracts shared by the supervised AgentRuntime components."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tradingagents.agent_harness.core.tier import Intent, Op
from tradingagents.agent_harness.tools.permission import PermissionType


class ContractModel(BaseModel):
    """Base for immutable runtime values with a closed schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentMessageType(str, Enum):
    TASK = "TASK"
    RESULT = "RESULT"
    QUESTION = "QUESTION"
    ANSWER = "ANSWER"
    HANDOFF_REQUEST = "HANDOFF_REQUEST"
    REPAIR_REQUEST = "REPAIR_REQUEST"
    CANCEL = "CANCEL"
    PROGRESS = "PROGRESS"


class TaskState(str, Enum):
    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_MESSAGE = "WAITING_MESSAGE"
    WAITING_CHILD = "WAITING_CHILD"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"


class RunState(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    LEGACY_INTERRUPTED = "LEGACY_INTERRUPTED"


class RunKind(str, Enum):
    AGENT_ANALYSIS = "AGENT_ANALYSIS"
    SYSTEM_COMMAND = "SYSTEM_COMMAND"


class TaskKind(str, Enum):
    AGENT = "AGENT"
    SYSTEM_COMMAND = "SYSTEM_COMMAND"


class OperationState(str, Enum):
    PREPARED = "PREPARED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_SAFE_TO_RETRY = "FAILED_SAFE_TO_RETRY"
    INDETERMINATE = "INDETERMINATE"
    RETRY_AUTHORIZED = "RETRY_AUTHORIZED"
    FAILED = "FAILED"


class OutboxState(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


class DependencyMode(str, Enum):
    ON_SUCCESS = "ON_SUCCESS"
    ON_TERMINAL = "ON_TERMINAL"


class EvidenceRef(ContractModel):
    artifact_id: str
    producer_task_id: str
    source_type: Literal["tool", "agent", "user"]
    source_name: str
    content_sha256: str
    as_of: datetime | None


class VerifiedEvidenceRef(EvidenceRef):
    verification_task_id: str
    verification_level: Literal["L1", "L2", "L3"]
    verified_at: datetime


class TaskConstraints(ContractModel):
    timeout_seconds: float = Field(gt=0)
    max_execution_attempts: int = Field(ge=1)
    allowed_tools: list[str]
    allow_handoff: bool = True


class RunBudgets(ContractModel):
    max_dynamic_tasks: int = Field(default=6, ge=0)
    max_messages: int = Field(default=20, ge=0)
    max_handoff_depth: int = Field(default=2, ge=0)
    max_repairs_per_task: int = Field(default=1, ge=0)
    max_total_tokens: int = Field(ge=0)
    deadline_at: datetime


class AgentError(ContractModel):
    code: str
    category: Literal[
        "VALIDATION",
        "PROVIDER",
        "TOOL",
        "POLICY",
        "BUDGET",
        "TIMEOUT",
        "CANCELLED",
        "INTERNAL",
    ]
    retryable: bool
    summary: str


class AgentTask(ContractModel):
    task_id: str
    run_id: str
    turn_id: str
    parent_task_id: str | None
    sender: str
    recipient: str
    objective: str
    inputs: dict[str, Any]
    dependency_results: list[EvidenceRef]
    constraints: TaskConstraints
    required: bool = True
    execution_attempt: int = Field(default=1, ge=1)


class TaskPayload(ContractModel):
    kind: Literal["TASK"]
    task: AgentTask


class ResultPayload(ContractModel):
    kind: Literal["RESULT"]
    success: bool
    content: str
    artifact_refs: list[EvidenceRef]
    confidence: float | None = Field(ge=0.0, le=1.0)
    missing_items: list[str]
    errors: list[AgentError]


class AnswerPayload(ContractModel):
    kind: Literal["ANSWER"]
    question_message_id: str
    value: dict[str, Any] | str
    answered_by: str


class CancelPayload(ContractModel):
    kind: Literal["CANCEL"]
    reason_code: str
    summary: str
    requested_by: str


class QuestionPayload(ContractModel):
    kind: Literal["QUESTION"]
    question: str
    answer_schema: dict[str, Any]
    user_required: bool
    expires_at: datetime


class HandoffPayload(ContractModel):
    kind: Literal["HANDOFF_REQUEST"]
    required_capability: str
    objective: str
    evidence_refs: list[EvidenceRef]
    acceptance_criteria: list[str]
    excluded_agents: list[str] = Field(default_factory=list)
    on_reject: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"]


class RepairPayload(ContractModel):
    kind: Literal["REPAIR_REQUEST"]
    target_task_id: str
    repair_kind: Literal["DOMAIN_EVIDENCE", "SYNTHESIS"]
    missing_evidence: list[str]
    acceptance_criteria: list[str]
    rejected_artifact_ids: list[str]
    on_reject: Literal["FAIL_REQUESTER", "RESUME_REQUESTER"]


class ProgressPayload(ContractModel):
    kind: Literal["PROGRESS"]
    stage: str
    summary: str
    percent: int | None = Field(default=None, ge=0, le=100)


AgentPayload = Annotated[
    TaskPayload
    | ResultPayload
    | AnswerPayload
    | CancelPayload
    | QuestionPayload
    | HandoffPayload
    | RepairPayload
    | ProgressPayload,
    Field(discriminator="kind"),
]

AgentDraftPayload = Annotated[
    QuestionPayload | HandoffPayload | RepairPayload | ProgressPayload,
    Field(discriminator="kind"),
]


class AgentMessageDraft(ContractModel):
    recipient: str
    type: Literal[
        AgentMessageType.QUESTION,
        AgentMessageType.HANDOFF_REQUEST,
        AgentMessageType.REPAIR_REQUEST,
        AgentMessageType.PROGRESS,
    ]
    payload: AgentDraftPayload
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    reason_summary: str | None = Field(default=None, max_length=500)


class AgentReply(ContractModel):
    success: bool
    content: str
    structured_data: dict[str, Any] | None
    evidence: list[EvidenceRef]
    confidence: float | None = Field(ge=0.0, le=1.0)
    missing_items: list[str]
    outgoing: list[AgentMessageDraft]
    errors: list[AgentError]


class AgentMessage(ContractModel):
    message_id: str
    seq: int = Field(ge=1)
    run_id: str
    turn_id: str
    task_id: str
    parent_task_id: str | None
    sender: str
    recipient: str
    type: AgentMessageType
    payload: AgentPayload
    evidence_refs: list[EvidenceRef]
    causation_id: str | None
    correlation_id: str
    idempotency_key: str
    execution_attempt: int = Field(ge=1)
    created_at: datetime

    @model_validator(mode="after")
    def validate_payload_kind(self) -> AgentMessage:
        if self.type.value != self.payload.kind:
            raise ValueError("message type must match payload kind")
        return self


class PlanTask(ContractModel):
    task_key: str
    agent: str
    capability: str
    objective: str
    inputs: dict[str, Any]
    depends_on: list[str]
    required: bool = True
    dependency_mode: DependencyMode = DependencyMode.ON_SUCCESS
    timeout_seconds: float | None = Field(default=None, gt=0)


class PlanGraph(ContractModel):
    domain_tasks: list[PlanTask]
    budgets: RunBudgets


class RouteDecision(ContractModel):
    intent: Intent
    op: Op
    tier: Literal[1, 2, 3]
    symbols: list[str]
    carry_symbols: list[str]
    slots: dict[str, Any]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str
    route_kind: Literal["DIRECT_READ", "SYSTEM_COMMAND", "AGENT_ANALYSIS"]


class CommandSpec(ContractModel):
    command_id: str
    entity: Literal["watchlist", "note", "alert", "scheduled", "run", "report"]
    op: Literal["CREATE", "READ", "UPDATE", "DELETE", "LIST", "RUN", "BULK_DELETE"]
    tool_name: str
    args: dict[str, Any]
    permission: PermissionType
    requires_approval: bool
    result_view: str


class AddTaskOp(ContractModel):
    op: Literal["ADD_TASK"]
    task_key: str
    agent: str
    capability: str
    objective: str
    inputs: dict[str, Any]
    required: bool
    repair_of_task_id: str | None = None
    handoff_from_task_id: str | None = None


class AddDependencyOp(ContractModel):
    op: Literal["ADD_DEPENDENCY"]
    upstream: str
    downstream: str
    condition: DependencyMode


class WaitForTaskOp(ContractModel):
    op: Literal["WAIT_FOR_TASK"]
    waiting_task_id: str
    child_task_key: str
    failure_policy: Literal["FAIL_WAITER", "RESUME_WITH_FAILURE"]


GraphPatchOperation = Annotated[
    AddTaskOp | AddDependencyOp | WaitForTaskOp,
    Field(discriminator="op"),
]


class GraphPatch(ContractModel):
    patch_id: str
    run_id: str
    requested_by_task_id: str
    expected_revision: int = Field(ge=0)
    reason_code: Literal[
        "MISSING_EVIDENCE",
        "VERIFICATION_FAILED",
        "CAPABILITY_MISMATCH",
        "USER_CONSTRAINT",
    ]
    operations: list[GraphPatchOperation]


__all__ = [
    "AddDependencyOp",
    "AddTaskOp",
    "AgentDraftPayload",
    "AgentError",
    "AgentMessage",
    "AgentMessageDraft",
    "AgentMessageType",
    "AgentPayload",
    "AgentReply",
    "AgentTask",
    "AnswerPayload",
    "CancelPayload",
    "CommandSpec",
    "DependencyMode",
    "EvidenceRef",
    "GraphPatch",
    "GraphPatchOperation",
    "HandoffPayload",
    "OperationState",
    "OutboxState",
    "PlanGraph",
    "PlanTask",
    "ProgressPayload",
    "QuestionPayload",
    "RepairPayload",
    "ResultPayload",
    "RouteDecision",
    "RunBudgets",
    "RunKind",
    "RunState",
    "TaskConstraints",
    "TaskKind",
    "TaskPayload",
    "TaskState",
    "VerifiedEvidenceRef",
    "WaitForTaskOp",
]
