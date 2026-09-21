from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from tradingagents.agent_harness.core.tier import Intent, Op
from tradingagents.agent_harness.runtime.models import (
    AddDependencyOp,
    AddTaskOp,
    AgentError,
    AgentMessage,
    AgentMessageDraft,
    AgentMessageType,
    AgentPayload,
    AgentReply,
    AgentTask,
    AnswerPayload,
    CancelPayload,
    CommandSpec,
    DependencyMode,
    EvidenceRef,
    GraphPatch,
    HandoffPayload,
    OperationState,
    OutboxState,
    PlanGraph,
    PlanTask,
    ProgressPayload,
    QuestionPayload,
    RepairPayload,
    ResultPayload,
    RouteDecision,
    RunBudgets,
    RunKind,
    RunState,
    TaskConstraints,
    TaskKind,
    TaskPayload,
    TaskState,
    VerifiedEvidenceRef,
    WaitForTaskOp,
)
from tradingagents.agent_harness.tools.permission import PermissionType

NOW = datetime.now(timezone.utc)


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        artifact_id="artifact-1",
        producer_task_id="0199-runtime-task-id",
        source_type="tool",
        source_name="market.quote",
        content_sha256="a" * 64,
        as_of=NOW,
    )


def _constraints() -> TaskConstraints:
    return TaskConstraints(
        timeout_seconds=30,
        max_execution_attempts=2,
        allowed_tools=["market.quote"],
    )


def _task() -> AgentTask:
    return AgentTask(
        task_id="0199-runtime-task-id",
        run_id="run-1",
        turn_id="turn-1",
        parent_task_id=None,
        sender="runtime",
        recipient="data_agent",
        objective="Fetch a current quote",
        inputs={"symbol": "AAPL"},
        dependency_results=[],
        constraints=_constraints(),
        execution_attempt=1,
    )


def _budgets() -> RunBudgets:
    return RunBudgets(
        max_total_tokens=10_000,
        deadline_at=NOW + timedelta(minutes=5),
    )


def _message(payload, message_type: AgentMessageType | None = None) -> AgentMessage:
    return AgentMessage(
        message_id="message-1",
        seq=1,
        run_id="run-1",
        turn_id="turn-1",
        task_id="0199-runtime-task-id",
        parent_task_id=None,
        sender="data_agent",
        recipient="runtime",
        type=message_type or AgentMessageType(payload.kind),
        payload=payload,
        evidence_refs=[],
        causation_id=None,
        correlation_id="correlation-1",
        idempotency_key="idempotency-1",
        execution_attempt=1,
        created_at=NOW,
    )


def test_runtime_enum_contracts_are_complete():
    assert {item.value for item in AgentMessageType} == {
        "TASK",
        "RESULT",
        "QUESTION",
        "ANSWER",
        "HANDOFF_REQUEST",
        "REPAIR_REQUEST",
        "CANCEL",
        "PROGRESS",
    }
    assert {item.value for item in TaskState} == {
        "PLANNED",
        "READY",
        "RUNNING",
        "WAITING_MESSAGE",
        "WAITING_CHILD",
        "WAITING_APPROVAL",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "INDETERMINATE",
    }
    assert {item.value for item in RunState} == {
        "PLANNING",
        "RUNNING",
        "WAITING_USER",
        "WAITING_APPROVAL",
        "NEEDS_RECONCILIATION",
        "SUCCEEDED",
        "PARTIAL_SUCCESS",
        "FAILED",
        "CANCELLED",
        "LEGACY_INTERRUPTED",
    }
    assert {item.value for item in OperationState} == {
        "PREPARED",
        "WAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "EXECUTING",
        "SUCCEEDED",
        "FAILED_SAFE_TO_RETRY",
        "INDETERMINATE",
        "RETRY_AUTHORIZED",
        "FAILED",
    }
    assert {item.value for item in OutboxState} == {
        "PENDING",
        "CLAIMED",
        "DELIVERED",
        "DEAD",
    }
    assert {item.value for item in DependencyMode} == {"ON_SUCCESS", "ON_TERMINAL"}
    assert {item.value for item in RunKind} == {"AGENT_ANALYSIS", "SYSTEM_COMMAND"}
    assert {item.value for item in TaskKind} == {"AGENT", "SYSTEM_COMMAND"}


def test_route_decision_is_typed_and_bounds_confidence():
    decision = RouteDecision(
        intent=Intent.QUOTE,
        op=Op.READ,
        tier=1,
        symbols=["AAPL"],
        carry_symbols=[],
        slots={"currency": "USD"},
        confidence=0.95,
        reason_code="EXPLICIT_QUOTE",
        route_kind="DIRECT_READ",
    )

    assert decision.intent is Intent.QUOTE
    assert decision.op is Op.READ
    with pytest.raises(ValidationError):
        RouteDecision(**{**decision.model_dump(), "confidence": 1.01})


def test_runtime_task_uses_global_id_while_plan_uses_local_task_key():
    task = _task()
    plan_task = PlanTask(
        task_key="quote",
        agent="data_agent",
        capability="market.quote",
        objective="Fetch a current quote",
        inputs={"symbol": "AAPL"},
        depends_on=[],
    )

    assert task.task_id == "0199-runtime-task-id"
    assert plan_task.task_key == "quote"
    with pytest.raises(ValidationError):
        AgentTask(**task.model_dump(), task_key="quote")
    with pytest.raises(ValidationError):
        PlanTask(**plan_task.model_dump(), task_id="0199-runtime-task-id")


def test_agent_task_requires_a_positive_execution_attempt():
    assert _task().execution_attempt == 1
    with pytest.raises(ValidationError):
        AgentTask(**{**_task().model_dump(), "execution_attempt": 0})


def test_plan_graph_preserves_local_dependencies_and_rejects_invalid_mode():
    quote = PlanTask(
        task_key="quote",
        agent="data_agent",
        capability="market.quote",
        objective="Fetch quote",
        inputs={"symbol": "AAPL"},
        depends_on=[],
    )
    alpha = PlanTask(
        task_key="alpha",
        agent="alpha_agent",
        capability="alpha.calculate",
        objective="Calculate alpha",
        inputs={},
        depends_on=["quote"],
        required=False,
        dependency_mode=DependencyMode.ON_TERMINAL,
        timeout_seconds=60,
    )

    graph = PlanGraph(domain_tasks=[quote, alpha], budgets=_budgets())

    assert graph.domain_tasks[1].depends_on == ["quote"]
    with pytest.raises(ValidationError):
        PlanTask(**{**alpha.model_dump(), "dependency_mode": "ALWAYS"})


def test_evidence_and_verified_evidence_are_strict_contracts():
    evidence = _evidence()
    verified = VerifiedEvidenceRef(
        **evidence.model_dump(),
        verification_task_id="verifier-task-1",
        verification_level="L2",
        verified_at=NOW,
    )

    assert verified.artifact_id == evidence.artifact_id
    with pytest.raises(ValidationError):
        EvidenceRef(**evidence.model_dump(), unexpected="value")


def test_every_agent_payload_uses_its_kind_as_discriminator():
    error = AgentError(
        code="PROVIDER_TIMEOUT",
        category="PROVIDER",
        retryable=True,
        summary="Provider timed out",
    )
    payloads = [
        TaskPayload(kind="TASK", task=_task()),
        ResultPayload(
            kind="RESULT",
            success=True,
            content="Fetched quote",
            artifact_refs=[_evidence()],
            confidence=0.9,
            missing_items=[],
            errors=[],
        ),
        AnswerPayload(
            kind="ANSWER",
            question_message_id="question-1",
            value={"symbol": "AAPL"},
            answered_by="user",
        ),
        CancelPayload(
            kind="CANCEL",
            reason_code="USER_REQUEST",
            summary="User cancelled",
            requested_by="user",
        ),
        QuestionPayload(
            kind="QUESTION",
            question="Which symbol?",
            answer_schema={"type": "string"},
            user_required=True,
            expires_at=NOW + timedelta(minutes=5),
        ),
        HandoffPayload(
            kind="HANDOFF_REQUEST",
            required_capability="news.search",
            objective="Find relevant news",
            evidence_refs=[_evidence()],
            acceptance_criteria=["Current sources"],
            on_reject="FAIL_REQUESTER",
        ),
        RepairPayload(
            kind="REPAIR_REQUEST",
            target_task_id="0199-runtime-task-id",
            repair_kind="DOMAIN_EVIDENCE",
            missing_evidence=["Current quote"],
            acceptance_criteria=["As-of timestamp present"],
            rejected_artifact_ids=["artifact-1"],
            on_reject="RESUME_REQUESTER",
        ),
        ProgressPayload(kind="PROGRESS", stage="fetch", summary="Fetching", percent=50),
    ]
    adapter = TypeAdapter(AgentPayload)

    for payload in payloads:
        restored = adapter.validate_python(payload.model_dump(mode="json"))
        assert type(restored) is type(payload)
        assert _message(payload).type.value == payload.kind

    assert error.retryable is True


def test_result_and_reply_reject_invalid_confidence():
    with pytest.raises(ValidationError):
        ResultPayload(
            kind="RESULT",
            success=True,
            content="ok",
            artifact_refs=[],
            confidence=-0.01,
            missing_items=[],
            errors=[],
        )

    with pytest.raises(ValidationError):
        AgentReply(
            success=True,
            content="ok",
            structured_data=None,
            evidence=[],
            confidence=1.01,
            missing_items=[],
            outgoing=[],
            errors=[],
        )


def test_agent_reply_and_message_draft_preserve_typed_outgoing_messages():
    outgoing = AgentMessageDraft(
        recipient="runtime",
        type=AgentMessageType.PROGRESS,
        payload=ProgressPayload(kind="PROGRESS", stage="fetch", summary="Fetching"),
        reason_summary="Waiting for the market data provider",
    )
    reply = AgentReply(
        success=True,
        content="Fetched quote",
        structured_data={"price": 201.5},
        evidence=[_evidence()],
        confidence=0.9,
        missing_items=[],
        outgoing=[outgoing],
        errors=[],
    )

    assert reply.outgoing[0].payload.kind == "PROGRESS"


def test_agent_message_rejects_kind_mismatch_and_is_frozen():
    payload = ProgressPayload(kind="PROGRESS", stage="fetch", summary="ok")

    with pytest.raises(ValidationError):
        _message(payload, AgentMessageType.RESULT)

    message = _message(payload)
    with pytest.raises(ValidationError):
        message.seq = 2


def test_graph_patch_discriminates_all_operation_types():
    patch = GraphPatch(
        patch_id="patch-1",
        run_id="run-1",
        requested_by_task_id="verifier-task-1",
        expected_revision=2,
        reason_code="MISSING_EVIDENCE",
        operations=[
            AddTaskOp(
                op="ADD_TASK",
                task_key="repair-quote",
                agent="data_agent",
                capability="market.quote",
                objective="Refetch quote",
                inputs={"symbol": "AAPL"},
                required=True,
                repair_of_task_id="0199-runtime-task-id",
            ),
            AddDependencyOp(
                op="ADD_DEPENDENCY",
                upstream="repair-quote",
                downstream="verifier-task-1",
                condition=DependencyMode.ON_SUCCESS,
            ),
            WaitForTaskOp(
                op="WAIT_FOR_TASK",
                waiting_task_id="verifier-task-1",
                child_task_key="repair-quote",
                failure_policy="FAIL_WAITER",
            ),
        ],
    )

    restored = GraphPatch.model_validate(patch.model_dump(mode="json"))
    assert [type(operation) for operation in restored.operations] == [
        AddTaskOp,
        AddDependencyOp,
        WaitForTaskOp,
    ]


def test_command_spec_supports_system_command_runs_and_forbids_extra_fields():
    command = CommandSpec(
        command_id="command-1",
        entity="note",
        op="CREATE",
        tool_name="notes.create",
        args={"symbol": "AAPL", "body": "Review earnings"},
        permission=PermissionType.WRITE,
        requires_approval=True,
        result_view="note_confirmation",
    )

    assert RunKind.SYSTEM_COMMAND.value == "SYSTEM_COMMAND"
    assert TaskKind.SYSTEM_COMMAND.value == "SYSTEM_COMMAND"
    assert command.permission is PermissionType.WRITE
    with pytest.raises(ValidationError):
        CommandSpec(**command.model_dump(), execution_attempt=1)
