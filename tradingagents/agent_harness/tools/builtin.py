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

from pydantic import BaseModel, Field, field_validator

from tradingagents.data.responses import DataResponse
from tradingagents.data.providers.registry import get_active_provider, get_active_provider_name, get_provider
from tradingagents.agent_harness.observability.failover import ProviderFailover

from .context import ToolContext
from .permission import PermissionType
from .capabilities import Capability

from .schema import ToolSchema, SideEffectMode

# ----------------------------------------------------------------------
# Layer 1: read tools
# ----------------------------------------------------------------------


def _normalize_a_share_symbol(symbol: str | None) -> str | None:
    """Append a default A-share exchange suffix to a bare 6-digit ticker.

    The LLM planner often drops the ``.SS`` / ``.SZ`` suffix when users
    type a plain 6-digit code like ``513880`` (the canonical SHH listing
    of the Huaan Nikkei 225 ETF). Without the suffix every provider
    returns ``None`` and the tool bubbles up ``NO_DATA``.

    Heuristic — purely syntactic, no network call:

    * Already has a ``.`` → leave alone (Yahoo / IB / Crypto suffixes).
    * 6 digits → Shanghai (``600/601/603/605/688/689/5/9``) or
      Shenzhen (``000/001/002/003/300/301``) exchange suffix.
    * Anything else → leave alone.

    This runs *before* the provider chain so the cache key
    (``(symbol.upper(), asset_type, ...)``) sees the canonical form and
    avoids duplicate cache slots for ``513880`` and ``513880.SS``.
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    if "." in s or not s.isdigit() or len(s) != 6:
        return symbol
    if s[0] in "569":  # 6xxxxx / 9xxxxx / 5xxxxx → 上交所
        return s + ".SS"
    if s[0] in "023":  # 0xxxxx / 2xxxxx / 3xxxxx → 深交所
        return s + ".SZ"
    return symbol


class QuoteArgs(BaseModel):
    """Schema for get_quote. Either ``symbol`` (one) or ``symbols`` (many).

    LLM planners sometimes emit a list when they actually want a batch
    snapshot; accepting both avoids Pydantic validation errors and lets
    the tool fan out via the same ProviderFailover chain as
    ``get_quotes_batch``. When ``symbols`` is provided, ``symbol`` is
    ignored and the response is a ``BatchQuoteResult``.
    """

    symbol: Optional[str] = None
    symbols: Optional[list[str]] = None
    asset_type: Literal["stock", "crypto", "fund"] = "stock"

    @field_validator("symbols")
    @classmethod
    def _strip_blanks(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        cleaned = [s for s in (s.strip() for s in v) if s]
        return cleaned or None


class QuoteResult(BaseModel):
    """Quote snapshot exposed to LLM via harness tools.

    Previously only had 8 fields, silently dropping QuoteSnapshot\'s
    quantitative fields (pe_ratio, market_cap, turnover_rate, etc.)
    that EastMoney / AKShare actually return. The downstream
    synthesizer refused to do valuation work because it had no
    PE / PB / market-cap. Widening the surface here keeps the
    harness-tool contract stable (downstream code reads these names)
    while giving the LLM the inputs it needs.
    """

    symbol: str
    price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None
    change: Optional[float] = None
    change_pct: Optional[float] = None  # aliased from QuoteSnapshot.change_percent
    volume: Optional[int] = None
    turnover: Optional[float] = None
    volume_ratio: Optional[float] = None
    turnover_rate: Optional[float] = None
    market_cap: Optional[float] = None
    circulating_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    amplitude: Optional[float] = None
    as_of: Optional[datetime] = None
    provider: str
    currency: Optional[str] = None
    exchange: Optional[str] = None
    market_status: Optional[str] = None
    asset_name: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


async def get_quote(args: QuoteArgs) -> "QuoteResult | BatchQuoteResult":
    """Single- or batch-quote entry point.

    - When ``args.symbols`` is a non-empty list, fans out to the same
      ProviderFailover chain as ``get_quotes_batch`` and returns a
      ``BatchQuoteResult``. This lets LLM planners emit a list without
      hitting a Pydantic validation error.
    - When ``args.symbol`` is provided, returns a single ``QuoteResult``.
    - If neither is provided, raises a clear ValueError.
    """
    if args.symbols:
        return await get_quotes_batch(
            BatchQuoteArgs(symbols=args.symbols, asset_type=args.asset_type)
        )
    if not args.symbol:
        raise ValueError("get_quote requires either `symbol` or `symbols`")

    canonical_symbol = _normalize_a_share_symbol(args.symbol)
    fo = ProviderFailover(primary=get_active_provider_name())
    snap = fo.call("get_quote", canonical_symbol, args.asset_type)
    # EastMoney and AKShare put the ticker name in raw_summary; quote
    # providers typically expose .name too. Use raw_summary as a fallback
    # chain.
    asset_name = (
        getattr(snap, "name", None)
        or getattr(snap, "raw_summary", None)
    )
    return QuoteResult(
        symbol=snap.symbol if snap is not None else canonical_symbol,
        price=getattr(snap, "price", None),
        open=getattr(snap, "open", None),
        high=getattr(snap, "high", None),
        low=getattr(snap, "low", None),
        previous_close=getattr(snap, "previous_close", None),
        change=getattr(snap, "change", None),
        # NOTE: QuoteSnapshot uses ``change_percent``; we expose
        # ``change_pct`` to keep the harness-tool contract stable.
        change_pct=getattr(snap, "change_percent", None),
        volume=int(getattr(snap, "volume")) if getattr(snap, "volume", None) is not None else None,
        turnover=getattr(snap, "turnover", None),
        volume_ratio=getattr(snap, "volume_ratio", None),
        turnover_rate=getattr(snap, "turnover_rate", None),
        market_cap=getattr(snap, "market_cap", None),
        circulating_cap=getattr(snap, "circulating_cap", None),
        pe_ratio=getattr(snap, "pe_ratio", None),
        # pb_ratio is not in QuoteSnapshot — left None for now; downstream
        # can populate later or callers can read QuoteSnapshot directly.
        pb_ratio=None,
        amplitude=getattr(snap, "amplitude", None),
        as_of=getattr(snap, "as_of", None),
        provider=fo.last_used_name or fo.primary_name,  # last tried in chain (None if never raised)
        currency=getattr(snap, "currency", None),
        exchange=getattr(snap, "exchange", None),
        market_status=getattr(snap, "market_status", None),
        asset_name=asset_name,
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
            last_provider_name = fo.last_used_name or fo.primary_name
        asset_name = (
            getattr(snap, "name", None)
            or getattr(snap, "raw_summary", None)
        )
        out.append(
            QuoteResult(
                symbol=snap.symbol,
                price=getattr(snap, "price", None),
                open=getattr(snap, "open", None),
                high=getattr(snap, "high", None),
                low=getattr(snap, "low", None),
                previous_close=getattr(snap, "previous_close", None),
                change=getattr(snap, "change", None),
                change_pct=getattr(snap, "change_percent", None),
                volume=int(getattr(snap, "volume")) if getattr(snap, "volume", None) is not None else None,
                turnover=getattr(snap, "turnover", None),
                volume_ratio=getattr(snap, "volume_ratio", None),
                turnover_rate=getattr(snap, "turnover_rate", None),
                market_cap=getattr(snap, "market_cap", None),
                circulating_cap=getattr(snap, "circulating_cap", None),
                pe_ratio=getattr(snap, "pe_ratio", None),
                pb_ratio=None,
                amplitude=getattr(snap, "amplitude", None),
                as_of=getattr(snap, "as_of", None),
                provider=last_provider_name,
                currency=getattr(snap, "currency", None),
                exchange=getattr(snap, "exchange", None),
                market_status=getattr(snap, "market_status", None),
                asset_name=asset_name,
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
    canonical_symbol = _normalize_a_share_symbol(args.symbol)
    # The provider chain only knows ``get_candles(symbol, interval, start,
    # end)`` today; asset_type is fixed at ``stock``. Sending the extra
    # positional arg trips ``TypeError: takes 5 positional arguments but 6
    # were given`` on every registered provider.
    candles = fo.call(
        "get_candles",
        canonical_symbol, args.interval, args.start, args.end,
    )
    return HistoryResult(
        symbol=canonical_symbol,
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
    """Fundamentals payload returned by the harness ``get_fundamentals`` tool.

    §0.4.15 — extended with PB / ROE / EPS / 52w range / dividend
    yield / revenue / net income so the user-facing answer can show
    real numbers instead of all-null placeholders (the §0.4.10-era
    impl used ``getattr`` against ``AssetIdentity`` which never had
    any of these attributes and silently returned ``None`` for
    every numeric field). All optional — providers fill only what
    they have.
    """
    symbol: str
    name: Optional[str] = None
    exchange: Optional[str] = None
    currency: Optional[str] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    market_cap: Optional[float] = None
    circulating_cap: Optional[float] = None
    roe: Optional[float] = None
    revenue: Optional[float] = None
    net_income: Optional[float] = None
    eps: Optional[float] = None
    dividend_yield: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None
    provider: str
    payload: dict = Field(default_factory=dict)


async def get_fundamentals(args: FundamentalsArgs) -> FundamentalsResult:
    """§0.4.15 — call the provider's ``get_fundamentals`` instead of
    reading fields off :class:`AssetIdentity` (which never carried
    PE / PB / market_cap / ROE). Each provider overrides
    ``get_fundamentals`` to surface the metrics it actually has:
    akshare + eastmoney populate from their quote snapshot
    (A-share fundamentals); yfinance reads ``ticker.info`` (US
    fundamentals).
    """
    fo = ProviderFailover(primary=get_active_provider_name())
    snap = fo.call("get_fundamentals", args.symbol, args.asset_type)
    return FundamentalsResult(
        symbol=snap.symbol,
        name=snap.name,
        exchange=snap.exchange,
        currency=snap.currency,
        pe_ratio=snap.pe_ratio,
        pb_ratio=snap.pb_ratio,
        market_cap=snap.market_cap,
        circulating_cap=snap.circulating_cap,
        roe=snap.roe,
        revenue=snap.revenue,
        net_income=snap.net_income,
        eps=snap.eps,
        dividend_yield=snap.dividend_yield,
        fifty_two_week_high=snap.fifty_two_week_high,
        fifty_two_week_low=snap.fifty_two_week_low,
        provider=fo.last_used_name or fo.primary_name,
        payload=snap.payload or {},
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
    # W3-D2 E2: news_provider seam. Picks an active provider from
    # NEWS_PROVIDERS (stub / yfinance / alpha_vantage) and adapts the
    # NewsWindow schema back into the existing NewsItem tool
    # contract. Failures degrade to a stub item with a warning so the
    # LLM still gets *something* to look at.
    from tradingagents.data.providers.news_registry import (
        get_active_news_provider,
    )
    provider = get_active_news_provider()
    window = provider.get_news(
        args.symbol, days=args.days, asset_type="stock",
    )
    items: list[NewsItem] = []
    for art in window.items:
        items.append(
            NewsItem(
                title=art.title,
                url=art.url or "about:blank",
                published_at=art.published_at,
                sentiment=art.sentiment,
            )
        )
    if not items:
        # Empty window from real provider → still return a placeholder
        # so the synthesizer can continue (matches pre-seam behaviour).
        items.append(
            NewsItem(
                title=f"[stub] news for {args.symbol}",
                url="about:blank",
                published_at=datetime.now(timezone.utc),
                sentiment=0.0,
            )
        )
    return NewsResult(symbol=args.symbol, items=items)


class ListAlphaFactorsResult(BaseModel):
    factors: list[str]


async def list_alpha_factors() -> ListAlphaFactorsResult:
    """Return the list of alpha158 factor names from the local factor library."""
    # W3-D2 E3: enumerates the real alpha158 factor registry (numpy/pandas
    # math stays local). The seam is about *where OHLCV data comes from*;
    # the factor library is intrinsic.
    try:
        from tradingagents.dataflows.alpha_factors import list_factors
        names = [s.name for s in list_factors()]
    except Exception:
        # Preserve the pre-seam placeholder if the factor library is
        # unavailable for any reason (e.g. broken pandas install).
        names = ["alpha_001", "alpha_002", "alpha_003"]
    return ListAlphaFactorsResult(factors=names)


class ComputeAlphaFactorsArgs(BaseModel):
    symbol: str
    # §Step 41 — ``None`` (or empty) means "compute every factor
    # registered in the alpha158 library". Keeps the public surface
    # backward-compatible: existing callers passing an explicit list
    # still get exactly that subset.
    factors: list[str] | None = None


class ComputeAlphaFactorsResult(BaseModel):
    symbol: str
    values: dict[str, float]


async def compute_alpha_factors(args: ComputeAlphaFactorsArgs) -> ComputeAlphaFactorsResult:
    # §Step 41 — resolve the factor list: explicit list wins,
    # otherwise ask the alpha158 library for its full registry. This
    # keeps callers like Tier 1 short_circuit (which only pass symbol)
    # useful out of the box.
    from tradingagents.dataflows.alpha_factors import list_factors
    try:
        names = [s.name for s in list_factors()]
    except Exception:
        names = []
    factors = list(args.factors) if args.factors else names
    if not factors:
        factors = ["alpha_001", "alpha_002"]
    # W3-D2 E3: pull OHLCV from the active alpha_provider and compute
    # the requested factors via the local alpha158 library. When the
    # provider has no data (empty df) we fall back to the pre-seam
    # zero-values so the synthesizer still gets a stable shape.
    from tradingagents.data.providers.alpha_registry import get_active_alpha_provider
    provider = get_active_alpha_provider()
    df = provider.load_ohlcv(args.symbol, asset_type="stock")
    if df is None or df.empty:
        return ComputeAlphaFactorsResult(
            symbol=args.symbol, values={f: 0.0 for f in factors}
        )
    try:
        from tradingagents.dataflows.alpha_factors import compute_factors
        out = compute_factors(df, factors)
    except Exception:
        return ComputeAlphaFactorsResult(
            symbol=args.symbol, values={f: 0.0 for f in factors}
        )
    # compute_factors returns a DataFrame (one col per factor) keyed
    # by date. Take the most recent row as the current factor reading.
    values: dict[str, float] = {}
    for f in factors:
        if f in out.columns and not out[f].empty:
            last = out[f].dropna()
            values[f] = float(last.iloc[-1]) if not last.empty else 0.0
        else:
            values[f] = 0.0
    return ComputeAlphaFactorsResult(symbol=args.symbol, values=values)


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
    # W3-D2 E3: load OHLCV via the active alpha_provider and ask the
    # alpha158 library for IC / Rank IC of the requested factor. Falls
    # back to zero IC when the library / provider is unavailable.
    from tradingagents.data.providers.alpha_registry import get_active_alpha_provider
    provider = get_active_alpha_provider()
    df = provider.load_ohlcv(args.symbol, asset_type="stock")
    if df is None or df.empty:
        return EvaluateAlphaResult(
            symbol=args.symbol, factor=args.factor, ic=0.0, rank_ic=0.0,
        )
    try:
        from tradingagents.dataflows.alpha_factors import evaluate_factor
        out = evaluate_factor(df, args.factor, forward_days=args.horizon_days)
    except Exception:
        return EvaluateAlphaResult(
            symbol=args.symbol, factor=args.factor, ic=0.0, rank_ic=0.0,
        )
    return EvaluateAlphaResult(
        symbol=args.symbol,
        factor=args.factor,
        ic=float(out.get("ic", 0.0)),
        rank_ic=float(out.get("rank_ic", 0.0)),
    )


# ----------------------------------------------------------------------
# Layer 2: write tools (HITL — registry wires them through PermissionPolicy)
# ----------------------------------------------------------------------


class CreateAlertArgs(BaseModel):
    symbol: str
    kind: Literal["price_above", "price_below", "change_pct", "volume_spike"] = "price_above"
    params: dict = Field(default_factory=dict)
    asset_type: Literal["stock", "crypto", "fund"] = "stock"
    cooldown_seconds: int = 3600
    scope: str = "user"


class UpdateAlertArgs(BaseModel):
    alert_id: str
    enabled: Optional[bool] = None
    params: Optional[dict] = None
    cooldown_seconds: Optional[int] = None


class DeleteAlertArgs(BaseModel):
    alert_id: str


class DeleteAlertsForSymbolArgs(BaseModel):
    symbol: str
    asset_type: Literal["stock", "crypto"] = "stock"


class BulkSymbolArgs(BaseModel):
    """§P3-3+ — generic (symbol, asset_type) args for bulk-by-symbol
    operations: delete_notes_for_symbol,
    delete_scheduled_tasks_for_symbol.  Mirrors
    DeleteAlertsForSymbolArgs so the dispatch layer can use a
    consistent schema across the 3 bulk delete tools.
    """
    symbol: str
    asset_type: Literal["stock", "crypto"] = "stock"


class CreateNoteArgs(BaseModel):
    symbol: str
    body_md: str
    asset_type: Literal["stock", "crypto"] = "stock"
    scope: str = "user"


class UpdateNoteArgs(BaseModel):
    note_id: str
    body_md: str


class DeleteNoteArgs(BaseModel):
    note_id: str


# §P3-3 — CRUD args schemas for the entities the dispatch table covers
# (note / alert / scheduled / run / report).


class ListNotesArgs(BaseModel):
    symbol: Optional[str] = None
    limit: int = 50
    # §Step5.B — epoch-second window filter. Forwarded by the bridge
    # only when ``state.slots["time_range"]`` is present. Read tools
    # fall back to "all-time" when omitted, so this is purely additive.
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None


class ListNotesResult(BaseModel):
    text: str
    count: int = 0

    def display_view(self) -> dict:
        """Step 8 — UI-safe projection.

        Returns a small dict containing only the fields the
        frontend should render. Keeps ``text`` accessible so
        the UI can preview it; drops any future internal
        fields without breaking the contract.
        """
        preview = self.text[:240] if self.text else ""
        return {
            "summary": f"{self.count} 条记录" if self.count else "无记录",
            "preview": preview,
            "count": self.count,
        }


class ListAlertsArgs(BaseModel):
    symbol: Optional[str] = None
    include_disabled: bool = False
    limit: int = 50
    # §Step5.B — epoch-second window filter. Forwarded by the bridge
    # only when ``state.slots["time_range"]`` is present. Read tools
    # fall back to "all-time" when omitted, so this is purely additive.
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None


class ListAlertsResult(BaseModel):
    text: str
    count: int = 0

    def display_view(self) -> dict:
        """Step 8 — UI-safe projection.

        Returns a small dict containing only the fields the
        frontend should render. Keeps ``text`` accessible so
        the UI can preview it; drops any future internal
        fields without breaking the contract.
        """
        preview = self.text[:240] if self.text else ""
        return {
            "summary": f"{self.count} 条记录" if self.count else "无记录",
            "preview": preview,
            "count": self.count,
        }


class CreateScheduledTaskArgs(BaseModel):
    symbol: str
    asset_type: Literal["stock", "crypto"] = "stock"
    cron_expression: str = "0 9 * * 1-5"
    enabled: bool = True
    note: Optional[str] = None
    scope: str = "user"


class UpdateScheduledTaskArgs(BaseModel):
    job_id: str
    cron_expression: Optional[str] = None
    enabled: Optional[bool] = None
    note: Optional[str] = None
    scope: str = "user"


class DeleteScheduledTaskArgs(BaseModel):
    job_id: str
    scope: str = "user"


class RunScheduledTaskArgs(BaseModel):
    job_id: str


class RunTradingAgentsAnalysisArgs(BaseModel):
    symbol: str
    trade_date: Optional[str] = None
    asset_type: Literal["stock", "crypto"] = "stock"
    research_depth: int = 1
    scope: str = "user"
    # §P3-5 — when set, the new run is anchored on the conclusions
    # of a prior report: the prior signal / key numbers / summary
    # are injected into every analyst prompt and the resulting
    # complete_report.md gets a delta section that explicitly
    # answers "what changed since then". Pass ``None`` (omit) for a
    # standalone run.
    based_on_report_id: Optional[str] = None


class GetAnalysisStatusArgs(BaseModel):
    run_id: str


class CancelAnalysisRunArgs(BaseModel):
    run_id: str
    scope: str = "user"


class ListRunsArgs(BaseModel):
    status: Optional[str] = None
    limit: int = 20
    # §P3-3+ — focused symbol injection (carry-forward or explicit).
    # When set, the bridge uses RunManager.list_runs_for_ticker;
    # when empty, falls back to list_active_runs().
    symbol: Optional[str] = None


class ListRunsResult(BaseModel):
    text: str
    count: int = 0

    def display_view(self) -> dict:
        """Step 8 — UI-safe projection.

        Returns a small dict containing only the fields the
        frontend should render. Keeps ``text`` accessible so
        the UI can preview it; drops any future internal
        fields without breaking the contract.
        """
        preview = self.text[:240] if self.text else ""
        return {
            "summary": f"{self.count} 条记录" if self.count else "无记录",
            "preview": preview,
            "count": self.count,
        }


class ListReportsArgs(BaseModel):
    symbol: Optional[str] = None
    limit: int = 20
    # §Step5.B — epoch-second window filter. Forwarded by the bridge
    # only when ``state.slots["time_range"]`` is present. Read tools
    # fall back to "all-time" when omitted, so this is purely additive.
    since_ts: Optional[int] = None
    until_ts: Optional[int] = None


class ListReportsResult(BaseModel):
    text: str
    count: int = 0

    def display_view(self) -> dict:
        """Step 8 — UI-safe projection.

        Returns a small dict containing only the fields the
        frontend should render. Keeps ``text`` accessible so
        the UI can preview it; drops any future internal
        fields without breaking the contract.
        """
        preview = self.text[:240] if self.text else ""
        return {
            "summary": f"{self.count} 条记录" if self.count else "无记录",
            "preview": preview,
            "count": self.count,
        }


class GetReportArgs(BaseModel):
    report_id: str


class AddToWatchlistArgs(BaseModel):
    """Schema for add_to_watchlist."""

    symbol: str = Field(min_length=1)
    asset_type: Literal["stock", "crypto"] = "stock"
    note: Optional[str] = None


class AddToWatchlistResult(BaseModel):
    """Result of add_to_watchlist."""

    status: str  # "created" | "duplicate" | "error"
    symbol: str
    asset_type: str
    raw: str = ""


class RemoveFromWatchlistArgs(BaseModel):
    """Schema for remove_from_watchlist."""

    symbol: str = Field(min_length=1)
    asset_type: Literal["stock", "crypto"] = "stock"


class RemoveFromWatchlistResult(BaseModel):
    """Result of remove_from_watchlist."""

    status: str  # "deleted" | "not_found" | "error"
    symbol: str
    raw: str = ""


class ListWatchlistResult(BaseModel):
    """Markdown-formatted watchlist (from tools_bridge.list_watchlist)."""

    text: str
    count: int = 0


class ListScheduledTasksArgs(BaseModel):
    """§P3-3+ — focused symbol injection (carry-forward or explicit).

    When set, the bridge filters scheduler jobs by ticker; when
    empty, returns every job.
    """
    symbol: Optional[str] = None


class ListScheduledTasksResult(BaseModel):
    """Markdown / JSON-formatted scheduled-task list."""

    text: str
    count: int = 0

    def display_view(self) -> dict:
        """§Step 8 — UI-safe projection (see ListNotesResult for rationale)."""
        preview = self.text[:240] if self.text else ""
        return {
            "summary": f"{self.count} 条记录" if self.count else "无记录",
            "preview": preview,
            "count": self.count,
        }


_BRIDGE_PREFIXES = (
    "AWAITING_CONFIRMATION:",
    # notes
    "NOTE_CREATED:", "NOTE_UPDATED:", "NOTE_DELETED:",
    # alerts
    "ALERT_CREATED:", "ALERT_UPDATED:", "ALERT_DELETED:",
    # scheduled jobs (§12.1 — was missing, fell back to status=ok)
    "SCHEDULED_CREATED:", "SCHEDULED_UPDATED:", "SCHEDULED_DELETED:",
    # watchlist (§12.1 — ADDED/REMOVED/DUPLICATE from builtin.py:942+)
    "ADDED:", "REMOVED:", "DUPLICATE:",
    # generic
    "ERROR:", "NO_DATA:",
    "(no scheduled tasks)", "(\u7528\u6237\u5173\u6ce8\u5217\u8868\u4e3a\u7a7a)",
)


def _parse_bridge_text(text):
    """Map a tools_bridge return string to a harness tool result dict.

    Preserves the legacy ``{"status": "pending_approval"}`` shape so
    downstream orchestrator code that pattern-matches on status keeps
    working. The original bridge string is preserved in ``raw`` for
    audit / UI surfacing.
    """
    if not isinstance(text, str):
        return {"status": "ok", "raw": str(text)}
    for prefix in _BRIDGE_PREFIXES:
        if text.startswith(prefix):
            status_map = {
                "AWAITING_CONFIRMATION:": "pending_approval",
                "NOTE_CREATED:": "created",
                "NOTE_UPDATED:": "updated",
                "NOTE_DELETED:": "deleted",
                "ALERT_CREATED:": "created",
                "ALERT_UPDATED:": "updated",
                "ALERT_DELETED:": "deleted",
                "SCHEDULED_CREATED:": "created",
                "SCHEDULED_UPDATED:": "updated",
                "SCHEDULED_DELETED:": "deleted",
                "ADDED:": "created",
                "REMOVED:": "deleted",
                "DUPLICATE:": "duplicate",
                "ERROR:": "error",
                "NO_DATA:": "no_data",
                "(no scheduled tasks)": "empty",
                "(\u7528\u6237\u5173\u6ce8\u5217\u8868\u4e3a\u7a7a)": "empty",
            }
            parsed = {"status": status_map[prefix], "raw": text}
            break
    else:
        parsed = {"status": "ok", "raw": text}

    # §3.2 — attach user-facing summary so downstream renderers
    # (orchestrator._trivial_crud_summary + /confirm SSE emit) don't
    # have to re-parse raw. result_formatter is the single source of
    # truth for status → 中文短句 mapping.
    try:
        from tradingagents.agent_harness.core.result_formatter import summarize_tool_result
        summary = summarize_tool_result(parsed)
        if summary:
            parsed["summary"] = summary
    except Exception:
        # result_formatter is best-effort; never break tool invocation
        # because the formatter can't classify a new status.
        pass
    return parsed


async def _invoke_bridge(tool, kwargs, context):
    """Call a tools_bridge StructuredTool and parse the result."""
    import json as _json
    from langchain_core.runnables import RunnableConfig

    config = RunnableConfig(
        configurable={"thread_id": context.session_id if context else "default"}
    )
    try:
        result_text = await tool.ainvoke(kwargs, config=config)
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    parsed = _parse_bridge_text(result_text)
    if parsed["status"] == "pending_approval":
        try:
            payload = _json.loads(result_text[len("AWAITING_CONFIRMATION:"):].strip())
            parsed["gate"] = payload
        except Exception:
            pass
    return parsed


# ---------------------------------------------------------------------------
# Kind translation: harness-side schema → AlertRepository schema.
#
# AlertRepository only accepts kind in {"price", "quantitative"} with
# specific param shapes. tools_bridge.create_alert uses a different
# vocabulary (price_above / price_below / change_pct / volume_spike)
# that was never aligned with the repository — so any real write goes
# through the harness's translator below, NOT tools_bridge.create_alert.
# ---------------------------------------------------------------------------
def _translate_alert_args(args) -> tuple[str, dict]:
    """Map (kind, params) to (AlertRepository.kind, AlertRepository.params)."""
    params = dict(args.params or {})
    if args.kind == "price_above":
        return "price", {"threshold": params.get("threshold"), "direction": "above"}
    if args.kind == "price_below":
        return "price", {"threshold": params.get("threshold"), "direction": "below"}
    if args.kind == "change_pct":
        return "quantitative", {
            "metric": "change_percent",
            "change_pct": params.get("change_pct", 0),
            "window_minutes": params.get("window_minutes", 0),
        }
    if args.kind == "volume_spike":
        return "quantitative", {
            "metric": "volume",
            "change_pct": params.get("change_pct", 0),
            "window_minutes": params.get("window_minutes", 0),
        }
    raise ValueError(f"unsupported alert kind: {args.kind}")


async def _hitl_gate(
    session_id: str,
    tool_name: str,
    tool_args: dict,
    user_message: str | None = None,
) -> dict | None:
    """Return AWAITING_CONFIRMATION payload if not approved, else None."""
    from tradingagents.agent_harness import hitl as approval, audit as _audit
    if approval.is_approved(session_id, tool_name, tool_args):
        return None
    payload = {
        "needs_confirmation": True,
        "tool_name": tool_name,
        "tool_args": tool_args,
        "session_id": session_id,
    }
    # §Step 11 — surface a friendly impact_note alongside the
    # technical impact string. Frontend confirms use the friendly line
    # in the dialog header; ``impact`` stays for audit / debug.
    # §Step 13 — reason_short for the dialog header banner.
    try:
        from tradingagents.agent_harness.guardrails import (
            describe_impact, describe_reason_short,
        )
        impact = describe_impact(tool_name, tool_args, user_message)
        payload["impact"] = impact
        payload["impact_note"] = impact  # alias for cleaner frontend access
        payload["reason_short"] = describe_reason_short(tool_name)
    except Exception:
        payload["impact"] = f"Write operation: {tool_name}"
        payload["impact_note"] = payload["impact"]
        payload["reason_short"] = tool_name
    try:
        # §Step 12 — persist impact_note from the payload so the
        # audit viewer shows the same friendly line the user approved.
        payload["audit_id"] = _audit.log_write(
            tool_name=tool_name,
            tool_args=tool_args,
            status="pending",
            impact_note=payload.get("impact_note"),
        )
    except Exception:
        pass
    return payload


async def _hitl_consume(session_id: str, tool_name: str, tool_args: dict) -> None:
    from tradingagents.agent_harness import hitl as approval
    approval.consume_approval(session_id, tool_name, tool_args)


async def create_alert(args, context):
    """Create an alert via AlertRepository with kind translation + HITL gate."""
    from tradingagents.agent_harness.tools.impl import _get_repo
    import json as _json

    try:
        repo_kind, repo_params = _translate_alert_args(args)
    except ValueError as e:
        return {"status": "error", "raw": f"ERROR: {e}"}

    tool_args = {
        "symbol": args.symbol,
        "kind": args.kind,
        "params": dict(args.params or {}),
        "asset_type": args.asset_type,
        "cooldown_seconds": args.cooldown_seconds,
    }
    gate = await _hitl_gate(context.session_id, "create_alert", tool_args, getattr(context, "user_message", None))
    if gate is not None:
        return {
            "status": "pending_approval",
            "raw": "AWAITING_CONFIRMATION: " + _json.dumps(gate, ensure_ascii=False),
            "gate": gate,
        }

    try:
        repo = _get_repo("alerts")
        alert = repo.create(
            symbol=args.symbol,
            asset_type=args.asset_type,
            kind=repo_kind,
            params=repo_params,
            cooldown_seconds=args.cooldown_seconds,
        )
        await _hitl_consume(context.session_id, "create_alert", tool_args)
        return {
            "status": "created",
            "raw": "ALERT_CREATED: " + _json.dumps(
                {"id": alert.get("id"), "symbol": alert.get("symbol"), "kind": repo_kind},
                ensure_ascii=False,
            ),
        }
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}


async def update_alert(args, context):
    """Update an alert by ID (enabled / params / cooldown).

    Uses tools_bridge.update_alert directly (operates by alert_id, no
    schema mismatch to translate).
    """
    from tradingagents.agent_harness.tools.impl import update_alert as bridge
    kwargs = {"alert_id": args.alert_id}
    if args.enabled is not None:
        kwargs["enabled"] = args.enabled
    if args.params is not None:
        kwargs["params"] = dict(args.params)
    if args.cooldown_seconds is not None:
        kwargs["cooldown_seconds"] = args.cooldown_seconds
    return await _invoke_bridge(bridge, kwargs, context)


async def delete_alert(args, context):
    from tradingagents.agent_harness.tools.impl import delete_alert as bridge
    return await _invoke_bridge(bridge, {"alert_id": args.alert_id}, context)


async def delete_alerts_for_symbol(args: DeleteAlertsForSymbolArgs, context=None):
    from tradingagents.agent_harness.tools.impl import (
        delete_alerts_for_symbol as bridge,
    )
    return await _invoke_bridge(
        bridge,
        {"symbol": args.symbol, "asset_type": args.asset_type},
        context,
    )


async def delete_notes_for_symbol(args, context=None):
    """§P3-3+ — bulk soft-delete every note for a given symbol.
    One HITL dialog for the whole batch (handled by the bridge).
    """
    from tradingagents.agent_harness.tools.impl import delete_notes_for_symbol as bridge
    if isinstance(args, dict):
        symbol = (args.get("symbol") or "").strip()
        asset_type = (args.get("asset_type") or "stock").strip()
    elif args is None:
        symbol, asset_type = "", "stock"
    else:
        symbol = (getattr(args, "symbol", None) or "").strip()
        asset_type = (getattr(args, "asset_type", None) or "stock").strip()
    return await _invoke_bridge(
        bridge,
        {"symbol": symbol, "asset_type": asset_type},
        context,
    )


async def delete_scheduled_tasks_for_symbol(args, context=None):
    """§P3-3+ — bulk hard-delete every scheduled job for a given
    symbol. One HITL dialog for the whole batch.
    """
    from tradingagents.agent_harness.tools.impl import (
        delete_scheduled_tasks_for_symbol as bridge,
    )
    if isinstance(args, dict):
        symbol = (args.get("symbol") or "").strip()
        asset_type = (args.get("asset_type") or "stock").strip()
    elif args is None:
        symbol, asset_type = "", "stock"
    else:
        symbol = (getattr(args, "symbol", None) or "").strip()
        asset_type = (getattr(args, "asset_type", None) or "stock").strip()
    return await _invoke_bridge(
        bridge,
        {"symbol": symbol, "asset_type": asset_type},
        context,
    )


async def create_note(args, context):
    from tradingagents.agent_harness.tools.impl import create_note as bridge
    return await _invoke_bridge(
        bridge,
        {"symbol": args.symbol, "body_md": args.body_md, "asset_type": args.asset_type},
        context,
    )


async def update_note(args, context):
    from tradingagents.agent_harness.tools.impl import update_note as bridge
    return await _invoke_bridge(
        bridge, {"note_id": args.note_id, "body_md": args.body_md}, context
    )


async def delete_note(args, context):
    from tradingagents.agent_harness.tools.impl import delete_note as bridge
    return await _invoke_bridge(bridge, {"note_id": args.note_id}, context)


async def list_watchlist(args=None, context=None):
    from tradingagents.agent_harness.tools.impl import list_watchlist as bridge
    config = {"configurable": {"thread_id": context.session_id if context else "default"}}
    try:
        text = await bridge.ainvoke({}, config=config)
    except Exception as e:
        return ListWatchlistResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    count = 0
    if text.startswith("\u5171 "):
        try:
            count = int(text.split(" ", 2)[1])
        except (IndexError, ValueError):
            count = 0
    return ListWatchlistResult(text=text, count=count)


async def add_to_watchlist(args: AddToWatchlistArgs, context: ToolContext | None = None):
    """Add a symbol to the user's default watchlist.

    Delegates to :class:`WatchlistRepository.add_item` so the harness
    write path matches the web UI's mutation semantics (canonical
    ticker normalisation, UNIQUE index dedupe, version bump for
    optimistic concurrency).
    """
    import json as _json
    from tradingagents.agent_harness.tools.impl import _get_repo

    try:
        repo = _get_repo("watchlist")
    except RuntimeError as e:
        return AddToWatchlistResult(
            status="error", symbol=args.symbol, asset_type=args.asset_type,
            raw=f"ERROR: {e}",
        )

    try:
        item = repo.add_item(
            symbol=args.symbol,
            asset_type=args.asset_type,
            note=args.note,
            watchlist_id="default",
        )
        return AddToWatchlistResult(
            status="created", symbol=args.symbol,
            asset_type=args.asset_type,
            raw="ADDED: " + _json.dumps(
                {"id": item.get("id"), "symbol": item.get("symbol")},
                ensure_ascii=False,
            ),
        )
    except ValueError as e:
        # duplicate symbol or validation failure
        msg = str(e)
        if "duplicate" in msg.lower():
            return AddToWatchlistResult(
                status="duplicate", symbol=args.symbol,
                asset_type=args.asset_type,
                raw=f"DUPLICATE: {args.symbol}",
            )
        return AddToWatchlistResult(
            status="error", symbol=args.symbol, asset_type=args.asset_type,
            raw=f"ERROR: {e}",
        )
    except Exception as e:
        return AddToWatchlistResult(
            status="error", symbol=args.symbol, asset_type=args.asset_type,
            raw=f"ERROR: {type(e).__name__}: {e}",
        )


async def remove_from_watchlist(args: RemoveFromWatchlistArgs, context: ToolContext | None = None):
    """Remove a symbol from the user's default watchlist.

    Uses list_items to look up the item_id, then delete_item with
    ``expected_version`` for optimistic concurrency.  Returns
    ``not_found`` when the symbol isn't on the list (so the LLM can
    phrase the answer correctly without needing to parse a stack trace).
    """
    from tradingagents.agent_harness.tools.impl import _get_repo

    try:
        repo = _get_repo("watchlist")
    except RuntimeError as e:
        return RemoveFromWatchlistResult(
            status="error", symbol=args.symbol,
            raw=f"ERROR: {e}",
        )

    try:
        # Need the watchlist's current version for optimistic locking
        # in delete_item (UPDATE ... WHERE version=?).  ``get()``
        # initialises the default row on first access so this is
        # always safe to call.
        wl = repo.get(watchlist_id="default")
        wl_version = wl.get("version", 0)
        items = repo.list_items(watchlist_id="default")
        target = next(
            (it for it in items
             if it.get("symbol") == args.symbol
             and it.get("asset_type") == args.asset_type),
            None,
        )
        if target is None:
            return RemoveFromWatchlistResult(
                status="not_found", symbol=args.symbol,
                raw=f"NOT_FOUND: {args.symbol} not in watchlist",
            )
        repo.delete_item(
            item_id=target["id"],
            expected_version=wl_version,
        )
        return RemoveFromWatchlistResult(
            status="deleted", symbol=args.symbol,
            raw=f"REMOVED: {args.symbol}",
        )
    except Exception as e:
        return RemoveFromWatchlistResult(
            status="error", symbol=args.symbol,
            raw=f"ERROR: {type(e).__name__}: {e}",
        )


async def list_scheduled_tasks(args=None, context=None):
    from tradingagents.agent_harness.tools.impl import list_scheduled_tasks as bridge
    config = {"configurable": {"thread_id": context.session_id if context else "default"}}
    # §P3-3+ — focused symbol injection (carry-forward or explicit).
    # When set, the bridge filters scheduler jobs by ticker in
    # Python; when empty, returns all jobs.
    # Accept either a Pydantic args instance (post-coercion) or a raw
    # dict (pre-coercion paths) so this works through every dispatch
    # route (single CRUD, multi-CRUD, PTC, direct invoke).
    if isinstance(args, dict):
        sym = (args.get("symbol") or "").strip()
    elif args is None:
        sym = ""
    else:
        sym = (getattr(args, "symbol", None) or "").strip()
    payload = {} if not sym else {"symbol": sym}
    try:
        text = await bridge.ainvoke(payload, config=config)
    except Exception as e:
        return ListScheduledTasksResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    # The bridge now returns markdown; parse the count from the
    # "共 N 个定时任务:" header so the result object stays correct.
    import re as _re
    count = 0
    m = _re.search(r"共\s*(\d+)\s*个定时任务", text)
    if m:
        count = int(m.group(1))
    elif "无定时任务" in text:
        count = 0
    return ListScheduledTasksResult(text=text, count=count)


# §P3-3 — wrapper functions for the 9 new builtin tools (entity × op
# dispatch table references them; these wrap the LangChain @tool
# implementations in tools_bridge so the ToolRegistry can invoke them
# through the unified permission / pipeline path).


async def list_notes(args: ListNotesArgs | None = None, context=None):
    from tradingagents.agent_harness.tools.impl import list_notes as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    # Accept either a Pydantic args instance (post-coercion) or a raw
    # dict (pre-coercion paths).
    if isinstance(args, dict):
        symbol = (args.get("symbol") or "")
        limit = args.get("limit") or 50
    elif args is None:
        symbol, limit = "", 50
    else:
        symbol = (getattr(args, "symbol", None) or "")
        limit = getattr(args, "limit", 50) or 50
    payload = {"symbol": symbol, "limit": limit}
    try:
        text = await bridge.ainvoke(payload, config=cfg)
    except Exception as e:
        return ListNotesResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    return ListNotesResult(text=text, count=_parse_count_from_text(text))


async def list_alerts(args: ListAlertsArgs | None = None, context=None):
    from tradingagents.agent_harness.tools.impl import list_alerts as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    # Accept either a Pydantic args instance (post-coercion) or a raw
    # dict (pre-coercion paths).
    if isinstance(args, dict):
        symbol = (args.get("symbol") or "")
        include_disabled = bool(args.get("include_disabled", False))
        limit = args.get("limit") or 50
    elif args is None:
        symbol, include_disabled, limit = "", False, 50
    else:
        symbol = (getattr(args, "symbol", None) or "")
        include_disabled = bool(getattr(args, "include_disabled", False))
        limit = getattr(args, "limit", 50) or 50
    payload = {"symbol": symbol, "include_disabled": include_disabled, "limit": limit}
    try:
        text = await bridge.ainvoke(payload, config=cfg)
    except Exception as e:
        return ListAlertsResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    return ListAlertsResult(text=text, count=_parse_count_from_text(text))


async def list_runs(args: ListRunsArgs | None = None, context=None):
    from tradingagents.agent_harness.tools.impl import list_runs as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    # Accept either a Pydantic args instance (post-coercion) or a raw
    # dict (pre-coercion paths) so this works through every dispatch
    # route (single CRUD, multi-CRUD, PTC, direct invoke).
    if isinstance(args, dict):
        status = (args.get("status") or "")
        limit = args.get("limit") or 20
        symbol = (args.get("symbol") or "")
    elif args is None:
        status, limit, symbol = "", 20, ""
    else:
        status = (getattr(args, "status", None) or "")
        limit = getattr(args, "limit", 20) or 20
        # §P3-3+ — focused symbol injection (carry-forward or
        # explicit). When set, the bridge uses
        # RunManager.list_runs_for_ticker and returns ticker-scoped
        # results; when empty, falls back to list_active_runs().
        symbol = (getattr(args, "symbol", None) or "")
    payload = {"status": status, "limit": limit, "symbol": symbol}
    try:
        text = await bridge.ainvoke(payload, config=cfg)
    except Exception as e:
        return ListRunsResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    return ListRunsResult(text=text, count=_parse_count_from_text(text))


async def list_reports(args: ListReportsArgs | None = None, context=None):
    from tradingagents.agent_harness.tools.impl import list_reports as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    # Accept either a Pydantic args instance (post-coercion) or a raw
    # dict (pre-coercion paths).
    if isinstance(args, dict):
        symbol = (args.get("symbol") or "")
        limit = args.get("limit") or 20
    elif args is None:
        symbol, limit = "", 20
    else:
        symbol = (getattr(args, "symbol", None) or "")
        limit = getattr(args, "limit", 20) or 20
    payload = {"symbol": symbol, "limit": limit}
    try:
        text = await bridge.ainvoke(payload, config=cfg)
    except Exception as e:
        return ListReportsResult(text=f"ERROR: {type(e).__name__}: {e}", count=0)
    return ListReportsResult(text=text, count=_parse_count_from_text(text))


async def get_report(args: GetReportArgs, context=None):
    from tradingagents.agent_harness.tools.impl import get_report as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    try:
        text = await bridge.ainvoke({"report_id": args.report_id}, config=cfg)
    except Exception as e:
        import traceback as _tb
        # §Step 16 — log full traceback so we can see whether the error
        # comes from the tool body (impl.py) or from the LangChain wrapper.
        from tradingagents.agent_harness.core.logging import LOGGER as _L
        _L.warning("get_report failed: %s", _tb.format_exc())
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    return {"status": "ok", "text": text}


async def run_scheduled_task(args: RunScheduledTaskArgs, context=None):
    """§Trigger a scheduled task immediately — bridge wrapper.

    Note: this is a read-style 'fire-and-report' invocation, not a
    mutation, so it does NOT go through HITL.
    """
    from tradingagents.agent_harness.tools.impl import run_scheduled_task as bridge
    cfg = {"configurable": {"thread_id": context.session_id if context else "default"}}
    try:
        text = await bridge.ainvoke({"job_id": args.job_id}, config=cfg)
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    return {"status": "ok", "text": text}


async def run_trading_agents_analysis(args: RunTradingAgentsAnalysisArgs, context=None):
    """Start a TradingAgents run — read-style fire-and-return."""
    from tradingagents.agent_harness.tools.impl import run_trading_agents_analysis as bridge
    payload = {
        "symbol": args.symbol,
        "trade_date": args.trade_date or "",
        "asset_type": args.asset_type,
        "research_depth": int(args.research_depth or 1),
    }
    try:
        text = await bridge.ainvoke(payload)
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    return {"status": "ok", "text": text}


async def get_analysis_status(args: GetAnalysisStatusArgs, context=None):
    from tradingagents.agent_harness.tools.impl import get_analysis_status as bridge
    try:
        text = await bridge.ainvoke({"run_id": args.run_id})
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    return {"status": "ok", "text": text}


async def cancel_analysis_run(args: CancelAnalysisRunArgs, context=None):
    """Cancel a run — no HITL (idempotent, server-side cooperative flag)."""
    from tradingagents.agent_harness.tools.impl import cancel_analysis_run as bridge
    try:
        text = await bridge.ainvoke({"run_id": args.run_id})
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}
    return {"status": "ok", "text": text}


# §P3-3 — write tools (HITL). Mirror the create_alert / update_alert /
# delete_alert pattern: gate via _hitl_gate, mutate via the repository,
# consume via _hitl_consume.

async def create_scheduled_task(args: CreateScheduledTaskArgs, context):
    """Create a scheduled task via ScheduledJobRepository + HITL gate."""
    from tradingagents.agent_harness.tools.impl import _get_repo
    import json as _json

    tool_args = {
        "symbol": args.symbol, "asset_type": args.asset_type,
        "cron_expression": args.cron_expression,
        "enabled": bool(args.enabled), "note": args.note or "",
    }
    gate = await _hitl_gate(context.session_id, "create_scheduled_task", tool_args, getattr(context, "user_message", None))
    if gate is not None:
        return {
            "status": "pending_approval",
            "raw": "AWAITING_CONFIRMATION: " + _json.dumps(gate, ensure_ascii=False),
            "gate": gate,
        }
    try:
        repo = _get_repo("scheduled_jobs")
        job = repo.create(
            symbol=args.symbol, asset_type=args.asset_type,
            cron_expression=args.cron_expression, enabled=bool(args.enabled),
            note=args.note,
        )
        await _hitl_consume(context.session_id, "create_scheduled_task", tool_args)
        return {
            "status": "created",
            "raw": "SCHEDULED_CREATED: " + _json.dumps(
                {"id": job.get("id"), "symbol": job.get("symbol"),
                 "cron": job.get("cron_expression")},
                ensure_ascii=False,
            ),
        }
    except Exception as e:
        return {"status": "error", "raw": f"ERROR: {type(e).__name__}: {e}"}


async def update_scheduled_task(args: UpdateScheduledTaskArgs, context):
    """Update a scheduled task by ID — only non-None fields are applied."""
    from tradingagents.agent_harness.tools.impl import update_scheduled_task as bridge
    payload: dict = {"job_id": args.job_id}
    if args.cron_expression is not None:
        payload["cron_expression"] = args.cron_expression
    if args.enabled is not None:
        payload["enabled"] = bool(args.enabled)
    if args.note is not None:
        payload["note"] = args.note
    return await _invoke_bridge(bridge, payload, context)


async def delete_scheduled_task(args: DeleteScheduledTaskArgs, context):
    from tradingagents.agent_harness.tools.impl import delete_scheduled_task as bridge
    return await _invoke_bridge(bridge, {"job_id": args.job_id}, context)


def _parse_count_from_text(text: str) -> int:
    """Best-effort count extraction from a markdown-table response.

    Looks for the '共 N 条/个' prefix and returns N. Falls back to 0
    on any mismatch — callers should not rely on this for decisions,
    only for surface-level telemetry.
    """
    if not text:
        return 0
    try:
        import re as _re
        m = _re.search(r"共\s+(\d+)", text)
        if m:
            return int(m.group(1))
        # try JSON list
        import json as _json
        parsed = _json.loads(text)
        if isinstance(parsed, list):
            return len(parsed)
    except Exception:
        return 0
    return 0


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
        description=(
            "Get the latest quote snapshot for one symbol. "
            "For 2+ symbols in one call, prefer `get_quotes_batch` \u2014 "
            "it runs the provider chain once and returns a list. "
            "`get_quote` also accepts an optional `symbols` list and will "
            "internally dispatch to the batch path. Bare 6-digit A-share "
            "codes (e.g. `513880`) are auto-normalised to `513880.SS` / "
            "`.SZ`. For historical K-lines or a trend chart use "
            "`get_history` instead."
        ),
        args_schema=QuoteArgs,
        result_schema=QuoteResult,
        permission=PermissionType.READ,
        cache_ttl_seconds=60,
        # §Step 23 — D2 Tool metadata: capability tags + display_view
        # hint. The display_view helper picks the right renderer when
        # the result lands in the agent_final bubble.
        metadata={
            "capabilities": [Capability.QUOTE.value],
            "display_view": Capability.QUOTE.value,
            "category": "data",
        },
    )(get_quote)

    registry.register(
        name="get_quotes_batch",
        description=(
            "Get quote snapshots for many symbols in one call. Use this "
            "instead of multiple `get_quote` calls when the user asks "
            "about several tickers or a market overview."
        ),
        args_schema=BatchQuoteArgs,
        result_schema=BatchQuoteResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.QUOTE.value],
            "display_view": "quote",
            "category": "data",
        },
        cache_ttl_seconds=60,
    )(get_quotes_batch)

    registry.register(
        name="get_history",
        description=(
            "Get historical OHLCV candles (K-lines) for one symbol over a "
            "time window. **Use this for price trends, charts, \"\u6700\u8fd1 N \u5929\", "
            "\"\u5386\u53f2 K \u7ebf\", \"\u8d70\u52bf\", MA / RSI / \u632f\u5e45 trend analysis, "
            "and multi-day comparisons.** For a current single-ticker "
            "snapshot use `get_quote` instead."
        ),
        args_schema=HistoryArgs,
        result_schema=HistoryResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.HISTORY.value],
            "display_view": "history",
            "category": "data",
        },
        cache_ttl_seconds=120,
    )(get_history)

    registry.register(
        name="get_fundamentals",
        description="Get fundamental ratios (PE/PB/MarketCap/ROE) for one symbol.",
        args_schema=FundamentalsArgs,
        result_schema=FundamentalsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.FUNDAMENTALS.value],
            "display_view": "fundamentals",
            "category": "data",
        },
        cache_ttl_seconds=300,
    )(get_fundamentals)

    registry.register(
        name="get_news",
        description="Get recent news headlines + sentiment for one symbol.",
        args_schema=NewsArgs,
        result_schema=NewsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.NEWS.value],
            "display_view": "news",
            "category": "data",
        },
        cache_ttl_seconds=180,
    )(get_news)

    registry.register(
        name="list_alpha_factors",
        description="List available alpha158 factors.",
        args_schema=type(None),
        result_schema=ListAlphaFactorsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.ALPHA.value],
            "display_view": "alpha_list",
            "category": "data",
        },
    )(list_alpha_factors)

    registry.register(
        name="compute_alpha_factors",
        description="Compute alpha158 factor values for one symbol.",
        args_schema=ComputeAlphaFactorsArgs,
        result_schema=ComputeAlphaFactorsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.ALPHA.value],
            # §Step 41 — alpha numeric values render via the
            # ``alpha`` renderer (markdown table). ``alpha_list`` is
            # for the bare factor-name catalogue (``list_alpha_factors``).
            "display_view": "alpha",
            "category": "data",
        },
        cache_ttl_seconds=600,
    )(compute_alpha_factors)

    registry.register(
        name="evaluate_alpha",
        description="Evaluate IC / Rank IC for a factor over a horizon.",
        args_schema=EvaluateAlphaArgs,
        result_schema=EvaluateAlphaResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.ALPHA.value],
            "display_view": "alpha_list",
            "category": "data",
        },
        cache_ttl_seconds=600,
    )(evaluate_alpha)

    registry.register(
        name="list_watchlist",
        description="List the user's current watchlist.",
        args_schema=type(None),
        result_schema=ListWatchlistResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.WATCHLIST.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_watchlist)

    # §P3-1 — write-side watchlist tools.  Required so the LLM agent
    # can answer "add this asset to my watchlist" with a real mutation
    # instead of just describing what it would do.  HITL gate is
    # enforced at the orchestrator level (stream_chat layers a confirm
    # for write tools); tool itself is permissive.
    registry.register(
        name="add_to_watchlist",
        description=(
            "Add a symbol to the user's default watchlist.  Required "
            "args: symbol (string, 6 digits for A-share or AAPL-style "
            "ticker), asset_type ('stock' | 'crypto'), optional note."
        ),
        args_schema=AddToWatchlistArgs,
        result_schema=AddToWatchlistResult,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.WATCHLIST.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(add_to_watchlist)

    registry.register(
        name="remove_from_watchlist",
        description=(
            "Remove a symbol from the user's default watchlist by "
            "symbol + asset_type.  Returns not_found if the symbol is "
            "not on the list."
        ),
        args_schema=RemoveFromWatchlistArgs,
        result_schema=RemoveFromWatchlistResult,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.WATCHLIST.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(remove_from_watchlist)

    registry.register(
        name="list_scheduled_tasks",
        description="List the user's scheduled tasks (optional ticker filter).",
        args_schema=ListScheduledTasksArgs,
        result_schema=ListScheduledTasksResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_scheduled_tasks)

    # ---- Layer 2: write tools (HITL) -------------------------------
    registry.register(
        name="create_alert",
        description="Create a price/change alert (HITL approval; real impl via tools_bridge).",
        args_schema=CreateAlertArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.ALERT.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(create_alert)
    registry.register(
        name="update_alert",
        description="Update an existing alert by ID (enabled/params/cooldown; HITL).",
        args_schema=UpdateAlertArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.ALERT.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(update_alert)
    registry.register(
        name="delete_alert",
        description="Delete an alert by ID (soft-delete; HITL).",
        args_schema=DeleteAlertArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.ALERT.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_alert)

    registry.register(
        name="delete_alerts_for_symbol",
        description="Bulk-delete all alerts for a symbol (list + soft_delete loop; "
                    "single HITL gate; for '把这个资产的告警都删了' requests).",
        args_schema=DeleteAlertsForSymbolArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.ALERT.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_alerts_for_symbol)

    registry.register(
        name="delete_notes_for_symbol",
        description="Bulk-delete all notes for a symbol (list + soft_delete loop; "
                    "single HITL gate; for '把这个资产的笔记都删了' requests).",
        args_schema=BulkSymbolArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.NOTE.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_notes_for_symbol)

    registry.register(
        name="delete_scheduled_tasks_for_symbol",
        description="Bulk-delete all scheduled jobs for a symbol (list + delete loop; "
                    "single HITL gate; HARD delete; for '把这个资产的定时任务都删了' requests).",
        args_schema=BulkSymbolArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_scheduled_tasks_for_symbol)

    registry.register(
        name="create_note",
        description="Create a research note (HITL; real impl via tools_bridge).",
        args_schema=CreateNoteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.NOTE.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(create_note)
    registry.register(
        name="update_note",
        description="Update a research note by ID (HITL).",
        args_schema=UpdateNoteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.NOTE.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(update_note)
    registry.register(
        name="delete_note",
        description="Delete a research note by ID (soft-delete; HITL).",
        args_schema=DeleteNoteArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.NOTE.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_note)

    # §P3-3 — read tools that fill gaps in the entity × op dispatch
    # (note / alert / scheduled / run / report).
    registry.register(
        name="list_notes",
        description="List the user's notes (optional ticker filter).",
        args_schema=ListNotesArgs,
        result_schema=ListNotesResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.NOTE.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_notes)

    registry.register(
        name="list_alerts",
        description="List the user's alerts (optional ticker / disabled filter).",
        args_schema=ListAlertsArgs,
        result_schema=ListAlertsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.ALERT.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_alerts)

    registry.register(
        name="list_runs",
        description="List analysis runs (optional status / ticker filter).",
        args_schema=ListRunsArgs,
        result_schema=ListRunsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.RUN.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_runs)

    registry.register(
        name="list_reports",
        description="List historical analysis reports (optional ticker filter).",
        args_schema=ListReportsArgs,
        result_schema=ListReportsResult,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.REPORT.value],
            "display_view": "list",
            "category": "crud",
        },
    )(list_reports)

    registry.register(
        name="get_report",
        description="Read one analysis report's full markdown + metadata by ID.",
        args_schema=GetReportArgs,
        result_schema=dict,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.REPORT_READ.value],
            "display_view": "report_read",
            "category": "crud",
        },
    )(get_report)

    # §P3-3 — read-style 'fire-and-report' tools (no HITL; run returns
    # run_id + initial status, doesn't block waiting for completion).
    registry.register(
        name="run_trading_agents_analysis",
        description=(
            "Start a TradingAgents analysis run. Returns run_id immediately "
            "(does not block). Use get_analysis_status to poll progress and "
            "list_reports once status=completed."
        ),
        args_schema=RunTradingAgentsAnalysisArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.RUN.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(run_trading_agents_analysis)

    registry.register(
        name="get_analysis_status",
        description="Poll the status of one analysis run by ID.",
        args_schema=GetAnalysisStatusArgs,
        result_schema=dict,
        permission=PermissionType.READ,
        metadata={
            "capabilities": [Capability.RUN.value],
            "display_view": "list",
            "category": "crud",
        },
    )(get_analysis_status)

    registry.register(
        name="cancel_analysis_run",
        description=(
            "Cooperative cancel of a queued/running analysis run. "
            "Idempotent — no HITL required."
        ),
        args_schema=CancelAnalysisRunArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.RUN.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(cancel_analysis_run)

    registry.register(
        name="run_scheduled_task",
        description="Immediately fire a scheduled task (do not wait for cron).",
        args_schema=RunScheduledTaskArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(run_scheduled_task)

    # §P3-3 — write tools (HITL). Mirror create_alert / update_alert /
    # delete_alert.
    registry.register(
        name="create_scheduled_task",
        description=(
            "Create a scheduled analysis task (HITL; real impl via "
            "tools_bridge)."
        ),
        args_schema=CreateScheduledTaskArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(create_scheduled_task)

    registry.register(
        name="update_scheduled_task",
        description="Update an existing scheduled task by ID (HITL).",
        args_schema=UpdateScheduledTaskArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(update_scheduled_task)

    registry.register(
        name="delete_scheduled_task",
        description="Delete a scheduled task by ID (HITL; hard delete).",
        args_schema=DeleteScheduledTaskArgs,
        result_schema=dict,
        permission=PermissionType.WRITE,
        metadata={
            "side_effect_mode": SideEffectMode.LOCAL_TRANSACTIONAL.value,
            "capabilities": [Capability.SCHEDULED.value],
            "display_view": "ack",
            "category": "crud",
        },
    )(delete_scheduled_task)


