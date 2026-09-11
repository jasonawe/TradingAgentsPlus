"""HarnessConfig — 配置驱动 schema (P1 stub)。"""
from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel, Field


class HarnessConfig(BaseModel):
    """Harness 全局配置 — YAML + env 驱动。

    P1 stub,后续 Phase 扩展字段:
    - P3: tool 子集
    - P4: tier 路由规则
    - P7: circuit_breaker / retry 策略
    """

    data_dir: Path = Field(default_factory=lambda: Path(os.path.expanduser("~/.tradingagents")))
    llm_provider: str = "minimax-cn"     # 兼容现有 .env
    llm_model: str = "MiniMax-M3"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        return cls(
            data_dir=Path(os.path.expanduser(os.environ.get("TRADINGAGENTS_DATA_DIR", "~/.tradingagents"))),
            llm_provider=os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "minimax-cn"),
            llm_model=os.environ.get("TRADINGAGENTS_LLM_MODEL", "MiniMax-M3"),
            log_level=os.environ.get("TRADINGAGENTS_LOG_LEVEL", "INFO"),
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        """从 YAML 加载 — 留 P2 实施。"""
        raise NotImplementedError("from_yaml 留 P2 — 当前 P1 用 from_env")
