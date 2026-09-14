"""HarnessConfig — 配置驱动 schema (P1 stub)。"""
from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field


class HarnessConfig(BaseModel):
    """Harness 全局配置 — YAML + env 驱动。

    P1 stub,后续 Phase 扩展字段:
    - P3: tool 子集
    - P4: tier 路由规则
    - P7: circuit_breaker / retry 策略
    """

    # YAML 里可声明任意嵌套段(如 retry / circuit_breaker / plugin_*),
    # 通过 ``extra="allow"`` 保留到 ``__pydantic_extra__``,便于 plugin / 用户配置扩展。
    model_config = ConfigDict(extra="allow")

    data_dir: Path = Field(default_factory=lambda: Path(os.path.expanduser("~/.tradingagents")))
    llm_provider: str = "minimax-cn"     # 兼容现有 .env
    llm_model: str = "MiniMax-M3"
    log_level: str = "INFO"

    # L3 LLM-judge 模型 (spec §D6 N89 fix):judge 不复用主 LLM,需更强模型
    # (GPT-4o / Claude Sonnet / Claude Opus)。judge_provider / judge_model
    # 不配置时复用主 LLM。
    judge_provider: str = ""
    judge_model: str = ""
    enable_l3: bool = False  # L3 LLM-judge 总开关 (UI toggle 同步)

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        return cls(
            data_dir=Path(os.path.expanduser(os.environ.get("TRADINGAGENTS_DATA_DIR", "~/.tradingagents"))),
            llm_provider=os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "minimax-cn"),
            llm_model=os.environ.get("TRADINGAGENTS_LLM_MODEL", "MiniMax-M3"),
            log_level=os.environ.get("TRADINGAGENTS_LOG_LEVEL", "INFO"),
            judge_provider=os.environ.get("TRADINGAGENTS_JUDGE_PROVIDER", ""),
            judge_model=os.environ.get("TRADINGAGENTS_JUDGE_MODEL", ""),
            enable_l3=os.environ.get("TRADINGAGENTS_ENABLE_L3", "0").lower() in {"1", "true", "yes", "on"},
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "HarnessConfig":
        """从 YAML 加载 — delegate 到 ``loader.from_yaml`` 走 env 覆盖路径。"""
        # Local import 避免循环 (loader.py 已 import schema.py)
        from .loader import from_yaml as _loader_from_yaml

        return _loader_from_yaml(path)
