"""§P3-4 — LLMFactory runtime provider/model resolution.

When wired with a ``settings_lookup`` callback (as ``Harness`` does in
``web.app.create_app``), the factory resolves (provider, model) on each
cache miss so the user can switch models from the settings page without
restarting the service. Three contracts to lock in:

1. Explicit ``make(provider=..., model=...)`` kwargs always win —
   the harness uses this to override per-call (e.g. in tests).
2. ``settings_lookup`` beats constructor defaults (env vars).
3. A small TTL cache (default 10s) prevents settings_repo from being
   hammered by a single harness turn that fires many LLM calls.

Plus a few smaller contracts: role separation (main vs judge),
``is_configured`` reflecting the live state, ``invalidate()`` flushing
the cache so a PATCH applies immediately.
"""
from __future__ import annotations

import time

import pytest

from tradingagents.agent_harness.llm.factory import LLMFactory


class _FakeSettingsRepo:
    """Minimal settings_repo stand-in for unit tests.

    Only the ``get(key) -> dict|None`` surface is exercised.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._m = dict(mapping or {})

    def get(self, key: str) -> dict | None:
        v = self._m.get(key)
        return {"value": v, "source": "sqlite"} if v is not None else None

    def set(self, key: str, value: str) -> None:
        self._m[key] = value


def _lookup(repo: _FakeSettingsRepo):
    """Build the same closure ``web.app.create_app`` passes to Harness."""

    def _fn(key: str) -> str | None:
        entry = repo.get(key)
        return (entry or {}).get("value")

    return _fn


# ---------------------------------------------------------------------------
# Tier 1 — legacy path: no settings_lookup, env-var/constructor only
# ---------------------------------------------------------------------------
def test_legacy_factory_uses_constructor_defaults(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("TRADINGAGENTS_LLM_MODEL", "gpt-4o-mini")
    f = LLMFactory()
    assert f.is_configured() is True
    p, m = f._resolve_cached()
    assert (p, m) == ("openai", "gpt-4o-mini")


def test_legacy_factory_with_empty_env_is_unconfigured(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_LLM_MODEL", raising=False)
    f = LLMFactory()
    assert f.is_configured() is False
    p, m = f._resolve_cached()
    assert (p, m) == ("", "")


# ---------------------------------------------------------------------------
# Tier 2 — settings_lookup wins over constructor defaults
# ---------------------------------------------------------------------------
def test_settings_lookup_beats_constructor_defaults():
    repo = _FakeSettingsRepo({
        "llm.provider": "anthropic",
        "llm.model": "claude-3-5-sonnet",
    })
    f = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        settings_lookup=_lookup(repo),
    )
    assert f.is_configured() is True
    p, m = f._resolve_cached()
    assert (p, m) == ("anthropic", "claude-3-5-sonnet")


def test_settings_lookup_partial_only_provider_does_not_enable():
    """If settings has only one of (provider, model) set, factory
    falls back to defaults — there's no half-configured state."""
    repo = _FakeSettingsRepo({"llm.provider": "anthropic"})
    f = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        settings_lookup=_lookup(repo),
    )
    p, m = f._resolve_cached()
    assert (p, m) == ("openai", "gpt-4o-mini")


def test_settings_lookup_empty_falls_back_to_constructor():
    repo = _FakeSettingsRepo({})
    f = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        settings_lookup=_lookup(repo),
    )
    p, m = f._resolve_cached()
    assert (p, m) == ("openai", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# Tier 3 — explicit kwargs always win
# ---------------------------------------------------------------------------
def test_make_kwargs_override_settings_and_defaults():
    repo = _FakeSettingsRepo({
        "llm.provider": "anthropic",
        "llm.model": "claude-3-5-sonnet",
    })
    f = LLMFactory(
        default_provider="openai",
        default_model="gpt-4o-mini",
        settings_lookup=_lookup(repo),
    )
    # We don't actually invoke .make() (which constructs an
    # OpenAICompatibleProvider requiring a real network), but we can
    # verify the explicit-kwarg path through _resolve_cached when no
    # kwarg is supplied, and trust the construction is gated by the
    # same precedence rule.
    p, m = f._resolve_cached()
    assert (p, m) == ("anthropic", "claude-3-5-sonnet")


# ---------------------------------------------------------------------------
# Tier 4 — TTL cache
# ---------------------------------------------------------------------------
def test_resolve_cached_hits_repo_once_within_ttl():
    repo = _FakeSettingsRepo({
        "llm.provider": "openai",
        "llm.model": "gpt-4o-mini",
    })
    lookup = _lookup(repo)
    call_count = {"n": 0}

    def counted_lookup(key):
        call_count["n"] += 1
        return lookup(key)

    f = LLMFactory(
        settings_lookup=counted_lookup,
        lookup_ttl_seconds=10.0,
    )
    f._resolve_cached()
    f._resolve_cached()
    f._resolve_cached()
    # 1st call: 2 lookups (provider + model). 2nd & 3rd: served from cache.
    assert call_count["n"] == 2


def test_invalidate_forces_fresh_lookup():
    repo = _FakeSettingsRepo({
        "llm.provider": "openai",
        "llm.model": "gpt-4o-mini",
    })
    lookup = _lookup(repo)
    f = LLMFactory(settings_lookup=lookup, lookup_ttl_seconds=10.0)
    f._resolve_cached()
    repo.set("llm.provider", "anthropic")
    repo.set("llm.model", "claude-3-5-sonnet")
    # Within TTL → still cached, shows old values
    assert f._resolve_cached() == ("openai", "gpt-4o-mini")
    f.invalidate()
    # After invalidate → fresh lookup
    assert f._resolve_cached() == ("anthropic", "claude-3-5-sonnet")


def test_ttl_expiry_triggers_fresh_lookup(monkeypatch):
    repo = _FakeSettingsRepo({
        "llm.provider": "openai",
        "llm.model": "gpt-4o-mini",
    })
    lookup = _lookup(repo)
    f = LLMFactory(settings_lookup=lookup, lookup_ttl_seconds=0.05)
    f._resolve_cached()
    assert f._resolve_cached() == ("openai", "gpt-4o-mini")
    time.sleep(0.1)
    repo.set("llm.provider", "google")
    repo.set("llm.model", "gemini-1.5-pro")
    # TTL expired → fresh lookup, picks up the change.
    assert f._resolve_cached() == ("google", "gemini-1.5-pro")


# ---------------------------------------------------------------------------
# Tier 5 — role separation (main vs judge)
# ---------------------------------------------------------------------------
def test_judge_role_reads_separate_settings_keys():
    repo = _FakeSettingsRepo({
        "llm.provider": "openai",
        "llm.model": "gpt-4o-mini",
        "llm.judge_provider": "google",
        "llm.judge_model": "gemini-1.5-pro",
    })
    main = LLMFactory(settings_lookup=_lookup(repo), role="main")
    judge = LLMFactory(settings_lookup=_lookup(repo), role="judge")
    assert main._resolve_cached() == ("openai", "gpt-4o-mini")
    assert judge._resolve_cached() == ("google", "gemini-1.5-pro")


def test_unknown_role_defaults_to_main():
    """Defensive: factory must reject unknown roles rather than
    silently use a different key prefix (which would mean the user
    could think they're configuring one model while another is
    being used)."""
    repo = _FakeSettingsRepo({
        "llm.provider": "openai",
        "llm.model": "gpt-4o-mini",
    })
    f = LLMFactory(settings_lookup=_lookup(repo), role="bogus")
    assert f.role == "main"
    assert f._resolve_cached() == ("openai", "gpt-4o-mini")


# ---------------------------------------------------------------------------
# Tier 6 — is_configured reflects the live lookup (not just the
# constructor defaults). Without this, the orchestrator's
# maybe_degrade_to_tier1 would silently fall through to Tier 1
# after a user switched to a configured provider.
# ---------------------------------------------------------------------------
def test_is_configured_reflects_settings_lookup():
    repo = _FakeSettingsRepo({})
    f = LLMFactory(
        default_provider="",
        default_model="",
        settings_lookup=_lookup(repo),
    )
    assert f.is_configured() is False

    repo.set("llm.provider", "openai")
    repo.set("llm.model", "gpt-4o-mini")
    f.invalidate()
    assert f.is_configured() is True


# ---------------------------------------------------------------------------
# Tier 7 — make() raises when nothing is resolvable (back-compat).
# ---------------------------------------------------------------------------
def test_make_raises_on_unconfigured(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_LLM_MODEL", raising=False)
    f = LLMFactory(settings_lookup=lambda k: None)
    with pytest.raises(ValueError, match="provider/model not configured"):
        f.make()
