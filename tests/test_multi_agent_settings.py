# tests/test_multi_agent_settings.py
import importlib

from tradingagents.agent_harness.runtime.multi_agent.settings import (
    RuntimeSettings, load_settings,
)

def test_runtime_settings_defaults():
    s = RuntimeSettings()
    assert s.multi_agent is False
    assert s.llm_budget_per_turn == 5
    assert s.max_hops == 8
    assert 0.0 <= s.consultation_rate_limit <= 0.8

def test_runtime_settings_override():
    s = RuntimeSettings(multi_agent=True, llm_budget_per_turn=2)
    assert s.multi_agent is True
    assert s.llm_budget_per_turn == 2

def test_load_settings_reads_from_set_config():
    from tradingagents.dataflows import config as cfg
    cfg.set_config({"runtime": {"multi_agent": True, "llm_budget_per_turn": 7}})
    s = load_settings()
    assert s.multi_agent is True
    assert s.llm_budget_per_turn == 7

def test_env_override_runtime_multi_agent(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_MULTI_AGENT", "true")
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_LLM_BUDGET_PER_TURN", "9")
    import tradingagents.default_config as dc
    importlib.reload(dc)
    import tradingagents.dataflows.config as dfcfg
    importlib.reload(dfcfg)
    cfg = dfcfg.get_config()
    assert cfg["runtime"]["multi_agent"] is True
    assert cfg["runtime"]["llm_budget_per_turn"] == 9
