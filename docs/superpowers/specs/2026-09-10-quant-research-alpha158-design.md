# 2026-09-10 — Quantitative Research Alpha158 Design

**Date:** 2026-09-10
**Stage:** B (Quant Research Dimension) — first of three sub-modules (B1/B2/B3)
**Status:** Draft — pending user review
**Branch:** `codex/quant-research-stage-b`
**Depends on:** vnpy.alpha 公式 + qlib alpha158(仅借鉴,不搬运代码)

## Background

Today the LangGraph analysts only use fundamental data, news, and a small set
of single technical indicators (`get_indicators` → MACD/RSI/etc., one vendor at a
time). They have no quantitative research vocabulary — no notion of "alpha",
no IC/IR for evaluating predictive power, no factor libraries, no
cross-sectional comparison. Even sophisticated users cannot ask the agent
*"which stocks in my watchlist rank highest on 20-day momentum adjusted for
volatility?"* and get a defensible answer.

We are in Stage B of the "理财通用 agent" plan. The three sub-modules are:

| Sub-module | 内容 | Status |
|------------|------|--------|
| **B1** Alpha158 因子库 | LangChain tool 暴露 30+ 量价/动量/趋势因子 + IC/IR 评估 | **This doc** |
| B2 paper_account 影子账户 | 本地模拟交易 | TBD |
| B3 BarGenerator 多周期 K 线 | tick/日线合成 5m/15m/30m/1h K 线 | TBD |

借鉴来源:
- vnpy.alpha — alpha158 公式 (MIT, 2024)
- qlib alpha158 — Microsoft Research 论文公式
- 仅搬因子计算 + 评估, **不搬 ML 训练框架**(我们有 LangGraph)

## Goal

Give LangGraph analysts (and the upcoming "通用 agent" orchestrator) a
defensible quantitative vocabulary:

1. **List factors** by category (momentum / volatility / volume-price / trend).
2. **Compute factor values** for any symbol × date range, with strict
   look-ahead bias prevention.
3. **Evaluate factor predictive power** via IC (Pearson + Rank IC) and IR
   over a forward N-day window.

When this lands, analysts can reason about *why* a stock moved, not just
*that* it moved, and downstream stages (debate room / PM) can use
factor-based evidence instead of hand-waving.

## Non-Goals

Explicit out-of-scope for B1:

- Multi-vendor OHLCV (yfinance only in B1; akshare provider comes in B1.1)
- Cross-sectional ranking (`screen_factors` tool — moved to B2 candidate)
- ML training / model fitting (we are not rebuilding vnpy.alpha's training
  loop)
- Factor persistence / caching layer (recompute on every call; ms cost is
  acceptable for 252-day windows)
- Web UI surface (no `web/static/alpha_factors.js`; analysts use these via
  tool calls; UI may come later if PM wants to see factor charts)
- Backtesting (`alpha_factors.evaluate_factor` measures predictive power but
  does **not** simulate a portfolio; that's B2's paper_account territory)

## User Stories

1. As a **fundamentals analyst agent**, when asked about a stock, I call
   `list_alpha_factors` to discover what's available, then call
   `compute_alpha_factors("600036.SS", "rsi_14,macd_hist,obv", "2026-09-10")`
   to get a recent snapshot, and cite these numbers in my reasoning.
2. As a **PM agent** considering multiple stocks, I can call
   `compute_alpha_factors` for each and compare `roc_20` (cross-sectional
   ranking is implicit in the returned values).
3. As a **debate-room LLM judge**, I call
   `evaluate_alpha("600036.SS", "rsi_14", forward_days=5)` to see if RSI
   has actually been a useful predictor for that symbol's next-5-day returns,
   rather than trusting the analyst's claim at face value.
4. As a **human user** debugging an analyst's reasoning, I can run the same
   tool from a Python REPL and confirm the numbers in the report.

## Functional Scope

### In Scope

| Item | Detail |
|------|--------|
| 因子数 | **31 个**,分 4 类 |
| 数据源 | **yfinance 单一入口**,通过 `stockstats_utils.load_ohlcv` 拉取 |
| 防 look-ahead | 复用 `load_ohlcv` 的 `curr_date` 过滤 |
| Tool 数 | **3 个** (`list_alpha_factors` / `compute_alpha_factors` / `evaluate_alpha`) |
| 评估指标 | IC (Pearson) + Rank IC (Spearman) + IC positive ratio + IR |
| 集成方式 | 新加 `alpha_factors` category 到 `dataflows/interface.py`,vendor = `yfinance` |
| 错误处理 | 因子未知/OHLCV 缺失/sample 不足统一返回 typed error |

### Out of Scope (this PR)

| Item | Reason |
|------|--------|
| akshare provider 适配 | B1.1 单独做(避免 B1 范围爆炸) |
| `screen_factors` 选股工具 | 留到 B2 评估时一起做(用户问 "哪些标的" 时一并解决) |
| 因子持久化/缓存 | 252 天 × 31 因子计算耗时 ms 级,不值得加缓存层 |
| ML 模型集成 | 不是 vnpy 借鉴目标,我们用 LangGraph |
| 因子回测引擎 | B2 paper_account 范围 |
| Web UI 展示 | B1 阶段不暴露,等用户实际用上后再考虑 |

## Architecture

### 模块划分

```
tradingagents/
├── dataflows/
│   ├── alpha_factors.py          [NEW] 因子计算 + IC/IR 评估(纯函数)
│   └── interface.py              [MOD] 加 alpha_factors category
└── agents/
    └── utils/
        └── alpha_factors_tools.py  [NEW] 3 个 @tool 暴露

tests/
└── test_alpha_factors.py         [NEW] 单元测试(因子正确性 + IC 评估)

docs/superpowers/specs/
└── 2026-09-10-quant-research-alpha158-design.md  [THIS]
```

### 数据流

```
Agent @tool 调用
    ↓
alpha_factors_tools.compute_alpha_factors(symbol, factors, curr_date)
    ↓
load_ohlcv(symbol, curr_date)       ← stockstats_utils,5 年本地缓存,防 look-ahead
    ↓ (DataFrame: OHLCV)
alpha_factors.compute_factors(df, factor_names)
    ↓ (逐因子调用注册的 FactorSpec.func)
pd.Series × N
    ↓
pd.DataFrame(因子值,index=Date)
    ↓
格式化输出(最近 30 行 + 汇总)
```

### 关键依赖

| 依赖 | 用途 | 已存在? |
|------|------|---------|
| `pandas` / `numpy` | DataFrame 计算 | ✅ 已有 |
| `stockstats_utils.load_ohlcv` | 5y OHLCV 拉取 + cache | ✅ 已有 |
| `langchain_core.tools.tool` | tool 装饰器 | ✅ 已有 |
| `tradingagents.dataflows.interface.route_to_vendor` | vendor 路由 | ⚠️ B1 不用,直接调 `load_ohlcv`(理由见下) |

**为什么 B1 不走 `route_to_vendor`?**

`route_to_vendor` 是为 formatted-string 输出设计的(给 LLM 看),内部做了 vendor fallback 和 NO_DATA 哨兵。但 alpha 因子需要 **原始 DataFrame** 才能算,字符串返回会损失精度和计算自由度。

折中方案:
- `load_ohlcv(symbol, curr_date)` 直接返回 DataFrame(已有缓存 + look-ahead 防护)
- 如果未来需要 akshare 数据源,在 `load_ohlcv` 加 vendor 参数,而不是让 alpha_factors 关心 vendor 路由

### 因子分类与命名

采用 **`<类别>_<周期>`** 命名法,与 vnpy/qlib 一致便于识别:

| 类别 | 因子(11+8+6+4=31) |
|------|----------------------|
| `momentum` 动量 | roc_1, roc_5, roc_10, roc_20, roc_60, momentum_5, momentum_10, momentum_20, rsi_6, rsi_12, rsi_24, macd, macd_signal, macd_hist |
| `volatility` 波动 | atr_14, std_5, std_10, std_20, histvol_20, histvol_60, boll_pct_b |
| `volume_price` 量价 | obv, vpt, pvt, vwap_5, vwap_20, volume_ratio_5 |
| `trend` 趋势 | adx_14, cci_20, aroon_up_25, aroon_down_25 |

完整公式参考:

- **ROC(N)** = `close.pct_change(N)` — N 日对数变化率
- **Momentum(N)** = `close - close.shift(N)` — N 日价格差
- **RSI(N)** Wilder 平滑,α=1/N
- **MACD** = EMA(12) - EMA(26)
- **MACD Signal** = EMA(9) of MACD
- **MACD Hist** = MACD - Signal
- **ATR(N)** = TR 的 Wilder 平滑,TR = max(H-L, |H-prev_C|, |L-prev_C|)
- **Std(N)** = close.rolling(N).std()
- **HistVol(N)** = std(log returns, N) × √252
- **Boll %b** = (Close - MA20) / (2 × Std20)
- **OBV** = cumsum(sign(ΔClose) × Volume)
- **VPT** = cumsum((ΔClose/Close.shift) × Volume)
- **VWAP(N)** = rolling sum(TP × Vol) / rolling sum(Vol),TP=(H+L+C)/3
- **Volume Ratio(N)** = Volume / rolling_mean(Volume, N)
- **ADX(N)** = Wilder 平滑的 DX,DX = 100 × |+DI - -DI| / (+DI + -DI)
- **CCI(N)** = (TP - MA(TP, N)) / (0.015 × MeanDev(TP, N))
- **Aroon Up(N)** = 100 × (N - days_since_high_in_N) / N
- **Aroon Down(N)** = 100 × (N - days_since_low_in_N) / N

## Interface Contract

### Tool 1: `list_alpha_factors`

```python
@tool
def list_alpha_factors(
    category: Optional[str] = None,  # momentum/volatility/volume_price/trend
) -> str:
    """列出所有 alpha158 因子"""
```

返回: 按类别分组的 markdown 列表,每行 `- \`<name>\`: <description>`。
错误: 未知 category → 返回 `ERROR: 未知类别 '<cat>',可选 [...]`

### Tool 2: `compute_alpha_factors`

```python
@tool
def compute_alpha_factors(
    symbol: str,           # ticker, 如 600036.SS
    factors: str,          # 逗号分隔,如 "roc_5,rsi_14,macd";类别宏 "all:momentum"
    curr_date: str,        # yyyy-mm-dd
    lookback_days: int = 252,  # 60-1260
) -> str:
    """计算 alpha158 因子值"""
```

返回格式:
```
symbol: 600036.SS
factors: roc_5, rsi_14, macd
date range: 2025-08-25 ~ 2026-09-10
valid rows: 248
latest values (2026-09-10):
  roc_5: 0.024500
  rsi_14: 62.450000
  macd: 0.152300

--- 最近 30 日 ---
date,roc_5,rsi_14,macd
2026-08-03,0.018200,55.3,0.102
...
```

错误:
- `ERROR: 必须至少指定一个因子`
- `ERROR: 未知类别 '<cat>'`
- `ERROR: 拉取 OHLCV 失败 - <reason>`
- `NO_DATA_AVAILABLE: 未找到 '<symbol>' 的行情数据`
- `NO_DATA_AVAILABLE: 所选因子 [..] 在 '<symbol>' 上无有效值(可能需要更长历史)`

### Tool 3: `evaluate_alpha`

```python
@tool
def evaluate_alpha(
    symbol: str,
    factor: str,           # 单个因子名
    curr_date: str,
    forward_days: int = 5, # 1-30
) -> str:
    """评估因子预测能力:IC/Rank IC/IC positive ratio/IR"""
```

返回格式:
```
因子评估: rsi_14 on 600036.SS
前瞻天数: 5
样本数: 248

  Pearson IC:    0.0342
  Rank IC:       0.0287
  IC 标准差:     0.1245
  IC 正向比率:   0.5800
  IR(IC 均值/标准差): 0.2747

解读:
  IC > 0.03 通常认为因子有弱预测能力
  IC > 0.05 中等预测能力
  IC > 0.10 强预测能力(实际中很罕见)
  IC positive ratio > 0.55 表示因子在多数月份稳定正向
```

错误: 同 Tool 2 + `sample 不足(<N>),无法评估...`

## Data Model

无需新表。因子计算是纯函数,输入是 OHLCV DataFrame(已有缓存),输出是
pd.Series/DataFrame,**不写入数据库**。

缓存:`load_ohlcv` 现有的 5 年 CSV 缓存复用,key 为 `{symbol}-YFin-data-{start}-{end}.csv`。

## Testing Plan

### 单元测试 (`tests/test_alpha_factors.py`)

1. **构造性测试**:用已知的 OHLCV(单调上涨/下跌/震荡),手算预期因子值,
   验证实现正确性
   - `roc_5` 在 5 日涨幅 5% 时应返回 0.05
   - `rsi_24` 在持续上涨序列应 > 70,在持续下跌应 < 30
   - `macd_hist` = macd - signal(数学定义)
   - `boll_pct_b` 在 Close=MA 时应 ≈ 0,在 ±2σ 时应 ≈ ±1
2. **IC 评估正确性**:
   - 用合成的"完美预测"序列(factor_t = forward_ret_t + noise),IC 应接近 1
   - 用完全无关的序列(随机),IC 应接近 0
3. **NaN 处理**:warmup 期应正确填充 NaN,不应抛异常
4. **错误路径**:未知因子、缺列、sample 不足、未知 symbol

### 集成测试 (手动)

1. **端到端**:从 Python REPL 调用三个 tool,验证 600036.SS 在 2026-09-10
   的 `rsi_14` 计算结果(预期 ~60-70 区间)
2. **LangGraph 集成**:在 `fundamentals_analyst` 临时加一行 tool,
   跑一次分析,确认 tool 注册成功且结果被引用
3. **回归**:跑 `tests/test_dataflows_config.py` 等现有测试,确保不破坏

## Migration / Rollout

无需数据库迁移(DB schema 不变)。

部署:
1. merge `codex/quant-research-stage-b` → main
2. 打 tag `v0.6.0`(alpha158)
3. 服务无需重启(纯 Python 模块加载)
4. 在 README `Web Platform` 一节加 alpha158 tool 用法示例

## Risk & Open Questions

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | `load_ohlcv` yfinance 对 A 股某些代码(如 688xxx 科创板、513xxx ETF)抓不到或字段不全 | B1 接受限制;B1.1 加 akshare 适配 A 股 |
| R2 | 因子公式与 vnpy.alpha 略有差异导致 IC 偏低 | 用经典量化教科书公式;reference 是 qlib 论文 |
| R3 | 30+ 因子全算一次慢 | 实测 252 天全因子 < 50ms(单 symbol);B1 不优化 |
| R4 | Agent 不知道何时调用 alpha tool | analyst prompt 里加一句"考虑调用 list_alpha_factors"提示(后续 PR) |
| O5 | 是否要立刻加 `screen_factors` 选股工具 | **等用户实际触发"哪些标的"问题再加**;不要先做 |

## Success Criteria

- [ ] 31 个因子全部正确实现(单元测试覆盖)
- [ ] IC 评估在合成数据上误差 < 1e-6
- [ ] 端到端:从 `tradingagents.agents.utils.alpha_factors_tools` 导入三个 tool,
      调用 `compute_alpha_factors("600036.SS", "rsi_14,macd_hist,obv", today)` 返回 CSV
- [ ] `dataflows/interface.py` 新加 `alpha_factors` category,不破坏现有 vendor 路由
- [ ] 不影响现有 12 个 analyst 的 tool 调用(回归测试通过)
- [ ] merge 后打 `v0.6.0` tag

## File Manifest

| File | Status | Lines (est) |
|------|--------|-------------|
| `tradingagents/dataflows/alpha_factors.py` | ✅ 草稿已落 | 447 |
| `tradingagents/agents/utils/alpha_factors_tools.py` | ✅ 草稿已落 | ~200 |
| `tradingagents/dataflows/interface.py` | 📝 修改(加 category) | +10 |
| `tests/test_alpha_factors.py` | 📝 新增 | ~250 |
| `docs/superpowers/specs/2026-09-10-quant-research-alpha158-design.md` | ✅ 本文件 | - |

## Next Steps (待用户确认)

1. 用户 review 本设计文档 → 批准 / 修订
2. 按设计实现 + 单元测试
3. 端到端验证(curl + REPL)
4. commit + push + merge main → 打 `v0.6.0` tag
5. 进入 B2 设计文档(paper_account)
