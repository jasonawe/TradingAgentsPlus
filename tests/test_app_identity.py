"""W3-D6 R8: AppIdentity User-Agent 强制。

覆盖:
  - 默认 identity (name / version / UA string)
  - env override (TRADINGAGENTS_APP_NAME / TRADINGAGENTS_APP_VERSION)
  - 自定义 user_agent 字段
  - frozen dataclass 不可改
  - headers() 返回的 3 个 key 形状
  - OpenAICompatibleProvider 自动注入 identity 到 default_headers
  - 显式 default_headers 优先于 identity
  - LLMFactory 把 identity 转发给 provider
  - Harness.app_identity 暴露给上层
"""
from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest


# ---------------------------------------------------------------------------
# AppIdentity dataclass
# ---------------------------------------------------------------------------


def test_default_identity_name_and_version():
    from tradingagents.agent_harness.llm.app_identity import (
        DEFAULT_NAME, DEFAULT_VERSION, AppIdentity,
    )
    identity = AppIdentity()
    assert identity.name == DEFAULT_NAME
    assert identity.version == DEFAULT_VERSION
    assert DEFAULT_NAME.startswith("TradingAgents")


def test_user_agent_defaults_to_name_slash_version():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(name="FooApp", version="1.2.3")
    assert identity.user_agent == "FooApp/1.2.3"


def test_custom_user_agent_overrides_default():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(
        name="FooApp", version="1.2.3",
        user_agent="FooApp-staging/1.2.3",
    )
    assert identity.user_agent == "FooApp-staging/1.2.3"


def test_str_returns_user_agent():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(name="BarApp", version="9.9")
    assert str(identity) == "BarApp/9.9"


def test_headers_shape_three_keys():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(name="BarApp", version="9.9")
    h = identity.headers()
    assert isinstance(h, dict)
    assert set(h.keys()) == {"User-Agent", "X-App-Name", "X-App-Version"}
    assert h["User-Agent"] == "BarApp/9.9"
    assert h["X-App-Name"] == "BarApp"
    assert h["X-App-Version"] == "9.9"


def test_headers_when_user_agent_overridden():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(
        name="BarApp", version="9.9",
        user_agent="BarApp-staging/9.9",
    )
    h = identity.headers()
    assert h["User-Agent"] == "BarApp-staging/9.9"
    assert h["X-App-Name"] == "BarApp"
    assert h["X-App-Version"] == "9.9"


def test_frozen_dataclass_rejects_mutation():
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(name="X", version="0.0.1")
    with pytest.raises(FrozenInstanceError):
        identity.name = "Y"  # type: ignore[misc]


def test_python_version_field_default_present():
    import platform as _p
    from tradingagents.agent_harness.llm import AppIdentity
    identity = AppIdentity(name="X", version="0")
    assert identity.python_version == _p.python_version()


# ---------------------------------------------------------------------------
# default_app_identity — env override
# ---------------------------------------------------------------------------


def test_default_app_identity_reads_env(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_APP_NAME", "CustomApp")
    monkeypatch.setenv("TRADINGAGENTS_APP_VERSION", "4.5.6")
    from tradingagents.agent_harness.llm import default_app_identity
    identity = default_app_identity()
    assert identity.name == "CustomApp"
    assert identity.version == "4.5.6"
    assert identity.user_agent == "CustomApp/4.5.6"


def test_default_app_identity_env_only_name(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_APP_NAME", "OnlyName")
    monkeypatch.delenv("TRADINGAGENTS_APP_VERSION", raising=False)
    from tradingagents.agent_harness.llm import default_app_identity
    identity = default_app_identity()
    assert identity.name == "OnlyName"
    # Version comes from package metadata or "dev" fallback
    assert identity.version
    assert "/" in identity.user_agent


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider — auto-inject identity headers
# ---------------------------------------------------------------------------


def _make_provider_with_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    from tradingagents.agent_harness.llm import OpenAICompatibleProvider
    return OpenAICompatibleProvider("openai", "gpt-4o-mini", api_key="dummy")


def test_provider_auto_injects_default_identity_headers(monkeypatch):
    provider = _make_provider_with_env(monkeypatch)
    inner = provider._client.get_llm()
    headers = inner.default_headers
    assert headers["User-Agent"].startswith("TradingAgentsPlus/")
    assert headers["X-App-Name"] == "TradingAgentsPlus"


def test_provider_explicit_identity_overrides_default(monkeypatch):
    from tradingagents.agent_harness.llm import AppIdentity, OpenAICompatibleProvider
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    custom = AppIdentity(name="CustomApp", version="9.9.9")
    provider = OpenAICompatibleProvider(
        "openai", "gpt-4o-mini", api_key="dummy", identity=custom,
    )
    inner = provider._client.get_llm()
    assert inner.default_headers["User-Agent"] == "CustomApp/9.9.9"
    assert inner.default_headers["X-App-Name"] == "CustomApp"


def test_provider_explicit_default_headers_wins(monkeypatch):
    from tradingagents.agent_harness.llm import OpenAICompatibleProvider
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    explicit = {"User-Agent": "CallerApp/1", "X-Custom": "yes"}
    provider = OpenAICompatibleProvider(
        "openai", "gpt-4o-mini", api_key="dummy",
        default_headers=explicit,
    )
    inner = provider._client.get_llm()
    # Caller's headers are forwarded verbatim (not merged / replaced).
    assert inner.default_headers == explicit


def test_provider_identity_property(monkeypatch):
    provider = _make_provider_with_env(monkeypatch)
    assert provider.identity.name.startswith("TradingAgents")
    assert "/" in provider.identity.user_agent


# ---------------------------------------------------------------------------
# LLMFactory — forward identity to providers
# ---------------------------------------------------------------------------


def test_llm_factory_forwards_identity(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    from tradingagents.agent_harness.llm import AppIdentity, LLMFactory
    custom = AppIdentity(name="FactoryApp", version="7.7")
    factory = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        identity=custom,
    )
    provider = factory.make()
    assert provider.identity.name == "FactoryApp"
    inner = provider._client.get_llm()
    assert inner.default_headers["User-Agent"] == "FactoryApp/7.7"


def test_llm_factory_caller_identity_overrides_factory(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    from tradingagents.agent_harness.llm import AppIdentity, LLMFactory
    factory = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        identity=AppIdentity(name="FactoryApp", version="1"),
    )
    provider = factory.make(identity=AppIdentity(name="CallerApp", version="2"))
    assert provider.identity.name == "CallerApp"


def test_llm_factory_default_identity_when_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("TRADINGAGENTS_APP_NAME", "EnvApp")
    monkeypatch.setenv("TRADINGAGENTS_APP_VERSION", "0.0.9")
    from tradingagents.agent_harness.llm import LLMFactory
    factory = LLMFactory(default_provider="openai", default_model="gpt-4o-mini")
    provider = factory.make()
    assert provider.identity.name == "EnvApp"
    assert provider.identity.version == "0.0.9"


# ---------------------------------------------------------------------------
# llm package exports
# ---------------------------------------------------------------------------


def test_llm_package_exports_app_identity():
    from tradingagents.agent_harness import llm
    assert hasattr(llm, "AppIdentity")
    assert hasattr(llm, "default_app_identity")
    assert "AppIdentity" in llm.__all__
    assert "default_app_identity" in llm.__all__


# ---------------------------------------------------------------------------
# openai_client.py: default_headers passes through _PASSTHROUGH_KWARGS
# ---------------------------------------------------------------------------


def test_default_headers_in_passthrough_tuple():
    from tradingagents.llm_clients.openai_client import _PASSTHROUGH_KWARGS
    assert "default_headers" in _PASSTHROUGH_KWARGS
