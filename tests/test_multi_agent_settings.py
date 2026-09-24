import pytest
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


# ----------------------------------------------------------------------
# §0.4.35 phase 2 — Work unit 4: rate limit + max depth validation
# ----------------------------------------------------------------------

def test_runtime_settings_consultation_max_depth_default():
    s = RuntimeSettings()
    assert s.consultation_max_depth == 3


def test_runtime_settings_consultation_max_depth_override():
    s = RuntimeSettings(consultation_max_depth=5)
    assert s.consultation_max_depth == 5


def test_runtime_settings_consultation_max_depth_out_of_range():
    """consultation_max_depth must be in [1, 10] — out-of-range raises
    SettingsError (alias of pydantic.ValidationError)."""
    from tradingagents.agent_harness.runtime.multi_agent.settings import (
        SettingsError,
    )
    with pytest.raises(SettingsError):
        RuntimeSettings(consultation_max_depth=0)
    with pytest.raises(SettingsError):
        RuntimeSettings(consultation_max_depth=11)


def test_runtime_settings_rate_limit_out_of_range():
    """consultation_rate_limit must be in [0.0, 0.8] — 0.9 raises
    SettingsError (alias of pydantic.ValidationError)."""
    from tradingagents.agent_harness.runtime.multi_agent.settings import (
        SettingsError,
    )
    with pytest.raises(SettingsError):
        RuntimeSettings(consultation_rate_limit=0.9)
    with pytest.raises(SettingsError):
        RuntimeSettings(consultation_rate_limit=-0.1)


def test_runtime_settings_rate_limit_boundary_values():
    """0.0 and 0.8 are both valid; 0.0 → ConsultBudgetExceeded at runtime."""
    s_low = RuntimeSettings(consultation_rate_limit=0.0)
    assert s_low.consultation_rate_limit == 0.0
    s_high = RuntimeSettings(consultation_rate_limit=0.8)
    assert s_high.consultation_rate_limit == 0.8
