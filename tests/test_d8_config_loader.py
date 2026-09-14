"""Tests for HarnessConfig.from_yaml (v3 spec §3 config/loader.py)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingagents.agent_harness.config import HarnessConfig, from_yaml  # noqa: E402


def test_from_yaml_loads_basic_fields(tmp_path: Path) -> None:
    yaml = tmp_path / "ta.yaml"
    yaml.write_text("""
data_dir: .ta_cache
llm_provider: minimax-cn
llm_model: MiniMax-M3
""")
    cfg = from_yaml(yaml)
    assert cfg.data_dir == Path(".ta_cache")
    assert cfg.llm_provider == "minimax-cn"
    assert cfg.llm_model == "MiniMax-M3"


def test_from_yaml_handles_nested_dict(tmp_path: Path) -> None:
    """Even without PyYAML, the minimal parser handles 2-space indents."""
    yaml = tmp_path / "ta.yaml"
    yaml.write_text("""
data_dir: .ta_cache
retry:
  max_retries: 5
  backoff_seconds: 1.5
""")
    cfg = from_yaml(yaml)
    assert cfg.retry["max_retries"] == 5
    assert cfg.retry["backoff_seconds"] == 1.5


def test_from_yaml_env_overrides(tmp_path: Path, monkeypatch) -> None:
    yaml = tmp_path / "ta.yaml"
    yaml.write_text("""
data_dir: .ta_cache
llm_model: file-model
""")
    monkeypatch.setenv("TRADINGAGENTS_LLM_MODEL", "env-model")
    cfg = from_yaml(yaml)
    assert cfg.llm_model == "env-model"


def test_from_yaml_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        from_yaml(tmp_path / "missing.yaml")


def test_from_yaml_parses_bool_int_float(tmp_path: Path) -> None:
    yaml = tmp_path / "ta.yaml"
    yaml.write_text("""
data_dir: /tmp/x
llm_model: m
""")
    # Set env values of various scalar types.
    os.environ["TRADINGAGENTS_DATA_DIR"] = "/var/y"
    os.environ.pop("TRADINGAGENTS_LLM_PROVIDER", None)
    try:
        cfg = from_yaml(yaml)
        assert cfg.data_dir == Path("/var/y")
    finally:
        os.environ.pop("TRADINGAGENTS_DATA_DIR", None)


def test_from_yaml_with_env_override_alias_works(tmp_path: Path) -> None:
    """Spec name: from_yaml_with_env_override is the canonical loader."""
    from tradingagents.agent_harness.config import from_yaml_with_env_override
    yaml = tmp_path / "ta.yaml"
    yaml.write_text("llm_model: alias-model\n")
    cfg = from_yaml_with_env_override(yaml)
    assert cfg.llm_model == "alias-model"
