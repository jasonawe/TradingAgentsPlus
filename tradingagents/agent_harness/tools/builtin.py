"""Built-in tools (v3 spec §5.1).

Spec splits these into 8 files (builtin_quote / builtin_history / ...
/ write_alert / write_note / write_scheduled). For maintainability we
keep them in a single file; each tool is independently registrable
through ``@tool_registry.register(...)`` so the split is purely lexical.

Layer 1 (read) — server-side, no LLM:
- get_quote, get_quotes_batch
- get_history
- get_fundamentals
- get_news
- list_alpha_factors, compute_alpha_factors, evaluate_alpha (3 alpha158 tools)

Layer 2 (write) — HITL required:
- create_alert, update_alert, delete_alert
- create_note, update_note, delete_note
- create_scheduled_task, update_scheduled_task, delete_scheduled_task
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from tradingagents.data.responses import DataResponse
from tradingagents.data.providers.registry import get_active_provider, get_active_provider_name, get_provider
from tradingagents.agent_harness.observability.failover import ProviderFailover

from .permission import PermissionType
from .schema import ToolSchema

# ----------------------------------------------------------------------
# Layer 1: read tools
# ----------------------------------------------------------------------


class QuoteArgs(BaseModel):
    symbol: str
    asset_type: Literal["stock", "crypto", "fund"] = "stock"


class QuoteResult(BaseModel):
    symbol: str
    price: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    as_of: Optional[datetime] = None
    provider: str
    warnings: list[str] = Field(default_factory=list)


async def get_quote(args: QuoteArgs) -> QuoteResult:
    # ProviderFailover walks the active provider first, then falls through
    # to yfinance/akshare/alpha_vantage on transient network errors. The
    # orchestrator layer (retry_async + skip_exceptions=ProviderError) is
    # configured NOT to retry provider-level failures, so this is the
    # single point that decides which upstream actually answers.
    fo = ProviderFailover(primary=get_active_provider_name())
    snap = fo.call("get_quote", args.symbol, args.asset_type)
    return QuoteResult(
        symbol=snap.symbol,
        price=getattr(snap, "price", None),
        change=getattr(snap, "change", None),
        change_pct=getattr(snap, "change_pct", None),
        volume=getattr(snap, "volume", None),
        as_of=getattr(snap, "as_of", None),
        provider=fo.last_used_name or fo.primary_name,  # last tried in chain (None if never raised)
        warnings=list(getattr(snap, "warnings", []) or []),
    )


class BatchQuoteArgs(BaseModel):
    symbols: list[str]
    asset_type: Literal["stock", "crypto", "fund"] = "stock"
    provider: Optional[str] = None


class BatchQuoteResult(BaseModel):
    quotes: list[QuoteResult]
    provider: str


async def get_quotes_batch(args: BatchQuoteArgs) -> BatchQuoteResult:
    # Explicit provider = honour the user\'s pick, no fallback.
    # Otherwise build one failover (chains the active provider first).
    explicit = get_provider(args.provider) if args.provider else None
    fo = None if explicit else ProviderFailover(primary=get_active_provider_name())
    out: list[QuoteResult] = []
    last_provider_name = explicit.name if explicit else None
    for sym in args.symbols:
        if explicit is not None:
            snap = explicit.get_quote(sym, args.asset_type)
            last_provider_name = explicit.name
        else:
            assert fo is not None
            snap = fo.call("get_quote", sym, args.asset_type)
            last_provider_name = fo.primary_name
        out.append(
            QuoteResult(
                symbol=snap.symbol,
                price=getattr(snap, "price", None),
                change=getattr(snap, "change", None),
                change_pct=getattr(snap, "change_pct", None),
                volume=getattr(snap, "volume", None),
                as_of=getattr(snap, "as_of", None),
                provider=last_provider_name,
                warnings=list(getattr(snap, "warnings", []) or []),
            )
        )
    return BatchQuoteResult(quotes=out, provider=last_provider_name or "?")


class HistoryArgs(BaseModel):
    symbol: str
    interval: Literal["1d", "1h", "5m"] = "1d"
    start: Optional[str] = None
    end: Optional[str] = None
    asset_type: Literal["stock", "crypto", "fund"] = "stock"


class CandleSchema(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class HistoryResult(BaseModel):
    symbol: str
    interval: str
    candles: list[CandleSchema]
    provider: str


async def get_history(args: HistoryArgs) -> HistoryResult:
    fo = ProviderFailover(primary=get_active_provider_name())
    candles = fo.call(
        "get_candles",
        args.symbol, args.interval, args.start, args.end, args.asset_type,
    )
    return HistoryResult(
        symbol=args.symbol,
        interval=args.interval,
        candles=[
            CandleSchema(
                timestamp=c.timestamp,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
            )
            for c in candles
        ],
        provider=fo.last_used_name or fo.primary_name,
    )


class FundamentalsArgs(BaseModel):
    symbol: str
    asset_type: Literal["stock", "crypto", "fund"] = "stock"


class FundamentalsResult(BaseModel):
    symbol: str
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    roe: Optional[float] = None
    provider: str


async def get_fundamentals(args: FundamentalsArgs) -> FundamentalsResult:
    fo = ProviderFailover(primary=get_active_provider_name())
    identity = fo.call("get_identity", args.symbol, args.asset_type)
    return FundamentalsResult(
        symbol=identity.symbol,
        pe_ratio=getattr(identity, "pe_ratio", None),
        pb_ratio=getattr(identity, "pb_ratio", None),
        market_cap=getattr(identity, "market_cap", None),
        roe=getattr(identity, "roe", None),
        provider=fo.last_used_name or fo.primary_name,
    )


class NewsArgs(BaseModel):
    symbol: str
    days: int = 7


class NewsItem(BaseModel):
    title: str
    url: str
    published_at: datetime
    sentiment: Optional[float] = None


class NewsResult(BaseModel):
    symbol: str
    items: list[NewsItem]


async def get_news(args: NewsArgs) -> NewsResult:
    # Stub: real implementation lives in dataflows/news.py — wrapped via plugin in P6.
    return NewsResult(
        symbol=args.symbol,
        items=[
            NewsItem(
                title=f"[stub] news for {args.symbol}",
                url="about:blank",
                published_at=datetime.now(timezone.utc),
                sentiment=0.0,
            )
        ],
    )


class ListAlphaFactorsResult(BaseModel):
    factors: list[str]


async def list_alpha_factors() -> ListAlphaFactorsResult:
    """Return the list of alpha158 factor names. Implemented in P5 (AlphaAgent)."""
    # Stub for P3; full implementation arrives with the AlphaAgent in P5.
    return ListAlphaFactorsResult(
        factors=["alpha_001", "alpha_002", "alpha_003"],
    )


class ComputeAlphaFactorsArgs(BaseModel):
    symbol: str
    factors: list[str] = Field(default_factory=lambda: ["alpha_001", "alpha_002"])


class ComputeAlphaFactorsResult(BaseModel):
    symbol: str
    values: dict[str, float]


async def compute_alpha_factors(args: ComputeAlphaFactorsArgs) -> ComputeAlphaFactorsResult:
    return ComputeAlphaFactorsResult(symbol=args.symbol, values={f: 0.0 for f in args.factors})


class EvaluateAlphaArgs(BaseModel):
    symbol: str
    factor: str
    horizon_days: int = 5


class EvaluateAlphaResult(BaseModel):
    symbol: str
    factor: str
    ic: float = 0.0
    rank_ic: float = 0.0


async def evaluate_alpha(args: EvaluateAlphaArgs) -> EvaluateAlphaResult:
    return EvaluateAlphaResult(
        symbol=args.symbol,
        factor=args.factor,
        ic=0.0,
        rank_ic=0.0,
    )


# ----------------------------------------------------------------------
# Layer 2: write tools (HITL — registry wires them through PermissionPolicy)
# ----------------------------------------------------------------------


class AlertWriteArgs(BaseModel):
    symbol: str
    kind: Literal["price", "change_pct"] = "price"
    threshold: float
    direction: Literal["above", "below"] = "above"
    scope: str = "user"


async def create_alert(args: AlertWriteArgs) -> dict:
    return {"status": "pending_approval", "scope": args.scope, "symbol": args.symbol}


async def update_alert(args: AlertWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "update", "scope": args.scope}


async def delete_alert(args: AlertWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "delete", "scope": args.scope}


class NoteWriteArgs(BaseModel):
    symbol: str
    body: str
    scope: str = "user"


async def create_note(args: NoteWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "create", "symbol": args.symbol}


async def update_note(args: NoteWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "update", "symbol": args.symbol}


async def delete_note(args: NoteWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "delete", "symbol": args.symbol}


class ScheduledWriteArgs(BaseModel):
    name: str
    cron: str
    payload: dict = Field(default_factory=dict)
    scope: str = "user"


async def create_scheduled_task(args: ScheduledWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "create", "name": args.name}


async def update_scheduled_task(args: ScheduledWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "update", "name": args.name}


async def delete_scheduled_task(args: ScheduledWriteArgs) -> dict:
    return {"status": "pending_approval", "action": "delete", "name": args.name}





class WatchlistItem(BaseModel):
    symbol: str
    asset_type: str
    note: str = ""


class ListWatchlistResult(BaseModel):
    items: list[WatchlistItem]


async def list_watchlist() -> ListWatchlistResult:
    """Return the user's current watchlist (stub; real impl reads web/state.db)."""
    return ListWatchlistResult(items=[])


class ScheduledTaskItem(BaseModel):
    name: str
    cron: str
    enabled: bool = True


class ListScheduledTasksResult(BaseModel):
    items: list[ScheduledTaskItem]


async def list_scheduled_tasks() -> ListScheduledTasksResult:
    """Return the user's scheduled tasks (stub; real impl reads web/state.db)."""
    return ListScheduledTasksResult(items=[])

# ----------------------------------------------------------------------
# Tool registry — one place to wire every builtin tool
# ----------------------------------------------------------------------


def install_builtin_tools(registry) -> None:
    """Register all builtin tools on ``registry``.

    Kept separate from the decorator form so callers can opt-out or
    pre-filter by permission level (e.g. dry-run servers skip writes).
    """
    registry.register(
        name="get_quote",
        description="Get the latest quote snapshot for one symbol.",
        args_schema=QuoteArgs,
        result_schema=QuoteResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=60,
    )(get_quote)

    registry.register(
        name="get_quotes_batch",
        description="Get quote snapshots for many symbols in one call.",
        args_schema=BatchQuoteArgs,
        result_schema=BatchQuoteResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=60,
    )(get_quotes_batch)

    registry.register(
        name="get_history",
        description="Get OHLCV candles for one symbol across an interval/window.",
        args_schema=HistoryArgs,
        result_schema=HistoryResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=120,
    )(get_history)

    registry.register(
        name="get_fundamentals",
        description="Get fundamental ratios (PE/PB/MarketCap/ROE) for one symbol.",
        args_schema=FundamentalsArgs,
        result_schema=FundamentalsResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=300,
    )(get_fundamentals)

    registry.register(
        name="get_news",
        description="Get recent news headlines + sentiment for one symbol.",
        args_schema=NewsArgs,
        result_schema=NewsResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=180,
    )(get_news)

    registry.register(
        name="list_alpha_factors",
        description="List available alpha158 factors.",
        args_schema=type(None),
        result_schema=ListAlphaFactorsResult,
        permission=PermissionType.READ,
    )(list_alpha_factors)

    registry.register(
        name="compute_alpha_factors",
        description="Compute alpha158 factor values for one symbol.",
        args_schema=ComputeAlphaFactorsArgs,
        result_schema=ComputeAlphaFactorsResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=600,
    )(compute_alpha_factors)

    registry.register(
        name="evaluate_alpha",
        description="Evaluate IC / Rank IC for a factor over a horizon.",
        args_schema=EvaluateAlphaArgs,
        result_schema=EvaluateAlphaResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=600,
    )(evaluate_alpha)

    registry.register(
        name="list_watchlist",
        description="List the user's current watchlist.",
        args_schema=type(None),
        result_schema=ListWatchlistResult,
        permission=PermissionType.READ,
    )(list_watchlist)

    registry.register(
        name="list_scheduled_tasks",
        description="List the user's scheduled tasks.",
        args_schema=type(None),
        result_schema=ListScheduledTasksResult,
        permission=PermissionType.READ,
    )(list_scheduled_tasks)

    # ---- Layer 2: write tools (HITL) -------------------------------
    registry.register(
        name="create_alert",
        description="Create a price/change alert (HITL approval).",
        args_schema=AlertWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(create_alert)
    registry.register(
        name="update_alert",
        description="Update an existing alert (HITL approval).",
        args_schema=AlertWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(update_alert)
    registry.register(
        name="delete_alert",
        description="Delete an alert (HITL approval).",
        args_schema=AlertWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(delete_alert)

    registry.register(
        name="create_note",
        description="Create a research note (HITL approval).",
        args_schema=NoteWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(create_note)
    registry.register(
        name="update_note",
        description="Update a research note (HITL approval).",
        args_schema=NoteWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(update_note)
    registry.register(
        name="delete_note",
        description="Delete a research note (HITL approval).",
        args_schema=NoteWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(delete_note)

    registry.register(
        name="create_scheduled_task",
        description="Create a scheduled analysis task (HITL approval).",
        args_schema=ScheduledWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(create_scheduled_task)
    registry.register(
        name="update_scheduled_task",
        description="Update a scheduled task (HITL approval).",
        args_schema=ScheduledWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(update_scheduled_task)
    registry.register(
        name="delete_scheduled_task",
        description="Delete a scheduled task (HITL approval).",
        args_schema=ScheduledWriteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
    )(delete_scheduled_task)
