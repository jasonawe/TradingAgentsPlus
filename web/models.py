"""Pydantic contracts shared by the web API, runner, and browser client."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from cli.models import AnalystType, AssetType
from cli.utils import (
    filter_analysts_for_asset_type,
    is_valid_ticker_input,
    normalize_ticker_symbol,
)


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PUBLISHING = "publishing"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"


class EventName(str, Enum):
    RUN_SNAPSHOT = "run_snapshot"
    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    AGENT_STATUS = "agent_status"
    PROGRESS = "progress"
    MESSAGE = "message"
    ACTIVITY = "activity"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_TIMED_OUT = "run_timed_out"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AnalysisRequest(BaseModel):
    """Validated, normalized input for one analysis run."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=32)
    analysis_date: date
    asset_type: AssetType = AssetType.STOCK
    analysts: list[AnalystType] = Field(min_length=1)
    research_depth: int
    output_language: str | None = Field(default=None, min_length=1, max_length=64)
    provider: str | None = Field(default=None, min_length=1, max_length=64)
    quick_model: str | None = Field(default=None, min_length=1, max_length=128)
    deep_model: str | None = Field(default=None, min_length=1, max_length=128)
    quote_strategy_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("ticker", mode="before")
    @classmethod
    def validate_ticker(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("ticker must be a string")
        value = value.strip()
        if not value or not is_valid_ticker_input(value):
            raise ValueError("ticker contains unsupported characters")
        return normalize_ticker_symbol(value)

    @field_validator("analysis_date", mode="before")
    @classmethod
    def validate_date(cls, value: Any) -> date:
        if isinstance(value, datetime):
            raise ValueError("analysis_date must be YYYY-MM-DD")
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not _DATE_RE.fullmatch(value.strip()):
            raise ValueError("analysis_date must be YYYY-MM-DD")
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError("analysis_date must be a valid calendar date") from exc

    @field_validator("research_depth")
    @classmethod
    def validate_depth(cls, value: int) -> int:
        if isinstance(value, bool) or value not in (1, 3, 5):
            raise ValueError("research_depth must be one of 1, 3, or 5")
        return value

    @field_validator("analysts")
    @classmethod
    def validate_analysts(cls, values: list[AnalystType]) -> list[AnalystType]:
        if len(values) != len(set(values)):
            raise ValueError("analysts must not contain duplicates")
        return values

    @model_validator(mode="after")
    def normalize_effective_analysts(self) -> AnalysisRequest:
        self.analysts = filter_analysts_for_asset_type(self.analysts, self.asset_type)
        if not self.analysts:
            raise ValueError("at least one analyst is required after asset filtering")
        return self


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    request: AnalysisRequest
    status: RunStatus = RunStatus.QUEUED
    phase: str | None = None
    current_agent: str | None = None
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    signal: str | None = None
    report_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    data_snapshot_id: str | None = None
    data_status: str | None = None
    reproducibility: str | None = None
    effective_quote_strategy_id: str | None = None
    effective_quote_provider_chain: list[str] = Field(default_factory=list)
    last_heartbeat_at: datetime | None = None
    timeout_at: datetime | None = None
    terminal_reason: str | None = None
    run_timeout_seconds: int | None = None
    run_heartbeat_interval_seconds: int | None = None
    run_heartbeat_timeout_seconds: int | None = None

    @computed_field
    @property
    def status_key(self) -> str:
        status = getattr(self.status, "value", self.status)
        return f"run_status.{status}"

    @model_validator(mode="after")
    def effective_metadata_from_request(self):
        if self.effective_quote_strategy_id is None:
            self.effective_quote_strategy_id = self.request.quote_strategy_id
        if not self.effective_quote_provider_chain and self.effective_quote_strategy_id:
            self.effective_quote_provider_chain = ["yfinance", "alpha_vantage"] if self.effective_quote_strategy_id == "fallback-yfinance-alpha-vantage" else ["yfinance"]
        if self.terminal_reason is not None:
            self.error_code = self.terminal_reason
        elif self.error_code is not None:
            self.terminal_reason = self.error_code
        return self


class HistoryRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    ticker: str
    analysis_date: date | None = None
    status: RunStatus | None = None
    asset_type: AssetType | None = None
    analysts: list[AnalystType] = Field(default_factory=list)
    research_depth: int | None = None
    generated_at: datetime | None = None
    signal: str | None = None
    report_id: str | None = None
    rating: str | None = None
    data_snapshot_id: str | None = None
    data_status: str | None = None
    reproducibility: str | None = None
    quote_strategy_id: str | None = None
    effective_quote_provider_chain: list[str] = Field(default_factory=list)


class EventPayload(BaseModel):
    """Base class for event payloads with legacy mapping-style access."""

    model_config = ConfigDict(extra="allow")

    _required_event_fields: ClassVar[tuple[str, ...]] = ()

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self):
        return self.model_dump().keys()

    def items(self):
        return self.model_dump().items()

    def __contains__(self, key: str) -> bool:
        return key in self.model_fields


class RunSnapshotPayload(EventPayload):
    run: RunRecord
    snapshot_seq: int = Field(default=0, ge=0)
    replay_from_seq: int | None


class RunStartedPayload(EventPayload):
    status: Literal["running"]
    ticker: str
    analysis_date: date
    asset_type: AssetType
    analysts: list[AnalystType]
    research_depth: Literal[1, 3, 5]


class PhaseChangedPayload(EventPayload):
    phase: str
    phase_index: int
    phase_count: int
    status: str


class AgentStatusPayload(EventPayload):
    agent: str
    status: Literal["pending", "in_progress", "completed"]


class ProgressPayload(EventPayload):
    progress: float = Field(ge=0.0, le=1.0)
    phase: str
    current_agent: str | None


class MessagePayload(EventPayload):
    message_type: str
    text: str


class ActivityPayload(EventPayload):
    activity_type: str
    name: str
    summary: str


class RunCompletedPayload(EventPayload):
    status: Literal["completed"]
    signal: str | None
    report_id: str


class RunFailedPayload(EventPayload):
    status: Literal["failed"]
    error_code: str
    error_message: str


class RunCancelledPayload(EventPayload):
    status: Literal["cancelled"]
    phase: str
    current_agent: str | None


class RunInterruptedPayload(EventPayload):
    status: Literal["interrupted"]
    error_code: Literal["service_restart"] = "service_restart"
    error_message: str


class RunTimedOutPayload(EventPayload):
    status: Literal["timed_out"]
    progress: float = Field(ge=0.0, le=1.0)
    terminal_reason: str
    error_code: str | None = None
    error_message: str

    @model_validator(mode="after")
    def mirror_terminal_reason(self):
        self.error_code = self.terminal_reason
        return self


_EVENT_PAYLOAD_MODELS: dict[EventName, type[EventPayload]] = {
    EventName.RUN_SNAPSHOT: RunSnapshotPayload,
    EventName.RUN_STARTED: RunStartedPayload,
    EventName.PHASE_CHANGED: PhaseChangedPayload,
    EventName.AGENT_STATUS: AgentStatusPayload,
    EventName.PROGRESS: ProgressPayload,
    EventName.MESSAGE: MessagePayload,
    EventName.ACTIVITY: ActivityPayload,
    EventName.RUN_COMPLETED: RunCompletedPayload,
    EventName.RUN_FAILED: RunFailedPayload,
    EventName.RUN_CANCELLED: RunCancelledPayload,
    EventName.RUN_INTERRUPTED: RunInterruptedPayload,
    EventName.RUN_TIMED_OUT: RunTimedOutPayload,
}


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    seq: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=datetime.now)
    event: EventName
    payload: EventPayload

    @field_serializer("payload")
    def serialize_payload(self, payload: EventPayload) -> dict[str, Any]:
        """Preserve concrete event fields when serializing the polymorphic payload."""

        return payload.model_dump(mode="json")

    @model_validator(mode="before")
    @classmethod
    def validate_event_payload(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        event = values.get("event")
        try:
            event_name = EventName(event)
        except (TypeError, ValueError):
            return values
        payload = values.get("payload")
        payload_model = _EVENT_PAYLOAD_MODELS.get(event_name)
        if payload_model is not None and not isinstance(payload, payload_model):
            values = dict(values)
            values["payload"] = payload_model.model_validate(payload)
        return values
