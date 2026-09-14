"""DataAgent — 行情 + 基本面 (v3 spec §4.1, N2/C1 fix).

合并 QuoteAgent + FundamentalsAgent; 不含 get_history (属于 AlphaAgent 前置)。

Tier 3 DAG 的并行节点之一;Tier 1 短路径**不**经过 DataAgent
(直接 PROVIDERS.get_quote)。
"""
from __future__ import annotations

from .base import AgentContext, AgentInput, AgentResult, BaseAgent


class DataAgent(BaseAgent):
    name = "data_agent"
    description = "Fetch quote + fundamentals for one symbol (no history)."
    tools: list = ["get_quote", "get_quotes_batch", "get_fundamentals"]
    system_prompt = "你是 DataAgent,擅长行情 + 基本面。"

    async def run(self, input: AgentInput, *, context: AgentContext) -> AgentResult:
        symbol = (input.context or {}).get("symbol") or input.plan_step.get("args", {}).get("symbol", "")
        return AgentResult(
            success=bool(symbol),
            content=f"data agent ready for {symbol or 'unknown symbol'}",
            structured_data={"symbol": symbol},
        )
