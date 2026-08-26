"""Pydantic contracts shared by the web API, runner, and browser client."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AnalysisRequest(BaseModel):
    """Validated, normalized input for one analysis run."""

    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=32)
    analysis_date: date
    asset_type: AssetType = AssetType.STOCK
    analysts: list[AnalystType] = Field(min_length=1)
    research_depth: int

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


class EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    seq: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=datetime.now)
    event: EventName
    payload: dict[str, Any] = Field(default_factory=dict)
