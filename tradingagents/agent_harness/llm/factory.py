"""LLMFactory — create_llm(provider_name, model_name) → LLMProvider (v3 spec §6 init order step 4).

§P3-4 — runtime-configurable provider/model via ``settings_lookup``.

The factory may be wired with a ``settings_lookup`` callable (e.g. a
closure over :class:`web.repositories.SettingsRepository`). On every
``.make()`` call the factory resolves the *effective* (provider,
model) tuple from, in priority order:

1. Explicit kwargs (``make(provider=..., model=...)``) — always wins.
2. ``settings_lookup("llm.provider")`` / ``settings_lookup("llm.model")``
   (or the judge-prefixed variant for :attr:`role` = ``"judge"``).
3. Constructor ``default_provider`` / ``default_model`` (which were
   themselves populated from env vars at startup).

A small TTL cache (10s by default) sits in front of the lookup so a
harness turn that fires N LLM calls in quick succession hits
``settings_repo`` only once. Once the TTL expires, the next call
re-reads the user's settings, picking up changes made via the
settings page without needing a process restart.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

from .app_identity import AppIdentity, default_app_identity
from .openai_provider import OpenAICompatibleProvider

LOGGER = logging.getLogger(__name__)


# Default TTL for the (provider, model) lookup cache. Long enough that
# a single harness turn (which can issue 5–10 LLM calls) reuses the
# resolved tuple; short enough that a user toggling the provider from
# the settings page sees the change at the next turn.
DEFAULT_LOOKUP_TTL_SECONDS = 10.0


class LLMFactory:
    """Build :class:`LLMProvider` instances from (provider, model) pairs.

    When ``settings_lookup`` is provided, the factory delegates the
    "what model am I on?" question to it on each cache miss, so the
    settings page can switch providers/models without restarting the
    service.
    """

    # §P3-4 mode split — main role resolves per-mode (quick vs deep)
    # with ``llm.model`` as the backward-compat fallback. Judge role
    # ignores ``mode`` (always single model).
    #
    # The dict shape is ``{role: (provider_key, {mode: model_key})}``.
    # For main role: mode="quick" → ``llm.quick_model`` (else
    # ``llm.model``); mode="deep" → ``llm.deep_model`` (else
    # ``llm.model``). For judge: any mode → ``llm.judge_model``.
    _ROLE_KEYS: dict[str, tuple[str, dict[str, str]]] = {
        "main": ("llm.provider", {
            "quick": "llm.quick_model",
            "deep": "llm.deep_model",
            # "unspecified" — explicit mode not declared by caller.
            # Falls back to the legacy single-model key so Phase 1
            # callers keep their existing behaviour.
            "unspecified": "llm.model",
        }),
        "judge": ("llm.judge_provider", {
            "quick": "llm.judge_model",
            "deep": "llm.judge_model",
            "unspecified": "llm.judge_model",
        }),
    }
    _VALID_MODES = frozenset({"quick", "deep", "unspecified"})

    def __init__(
        self,
        default_provider: str | None = None,
        default_model: str | None = None,
        cache: "LLMResponseCache | None" = None,
        identity: AppIdentity | None = None,
        *,
        settings_lookup: Callable[[str], str | None] | None = None,
        role: str = "main",
        lookup_ttl_seconds: float = DEFAULT_LOOKUP_TTL_SECONDS,
    ) -> None:
        self.default_provider = default_provider or os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "")
        self.default_model = default_model or os.environ.get("TRADINGAGENTS_LLM_MODEL", "")
        self.cache = cache
        # W3-D6 R8: process-wide identity, defaulted lazily so tests can
        # override env vars before the first call.
        self.identity = identity
        # §P3-4 — runtime-config lookup. ``None`` keeps the legacy
        # behaviour: factory only knows its constructor defaults (which
        # came from env vars at boot).
        self.settings_lookup = settings_lookup
        self.role = role if role in self._ROLE_KEYS else "main"
        self.lookup_ttl_seconds = lookup_ttl_seconds
        # §P3-4 mode split — the cache is keyed by mode so quick / deep
        # resolutions don't clobber each other within the same turn.
        # Stored as ``{mode: (provider, model, expires_at)}`` where
        # ``expires_at = 0.0`` means "no cached value yet" so the
        # first call for that mode always re-resolves.
        self._cache_by_mode: dict[str, tuple[str, str, float]] = {}

    def invalidate(self) -> None:
        """Drop the cached (provider, model) for *every* mode so the
        next :meth:`make` call re-reads settings. Useful for tests
        and for the PATCH endpoint to apply changes immediately
        rather than after TTL.
        """
        self._cache_by_mode.clear()

    def _resolve_cached(self, mode: str = "unspecified") -> tuple[str, str]:
        """Resolve the effective (provider, model) for this factory's
        role + mode. Cached for :attr:`lookup_ttl_seconds`.

        Resolution priority on each cache miss:

        1. ``settings_lookup(llm.provider)`` / ``settings_lookup(mode_key)``
           — wins over defaults when both are non-empty.
        2. ``self.default_provider`` / ``self.default_model`` — the
           constructor defaults, which already absorbed the env vars
           at ``__init__``.

        ``mode`` selects which settings key holds the model:

        * ``"quick"`` — cheap / summarisation calls. Reads
          ``llm.quick_model``; falls back to ``llm.model`` when
          ``quick_model`` is unset (Phase 1 backward compat).
        * ``"deep"`` — plan / synth / final-answer calls. Reads
          ``llm.deep_model``; falls back to ``llm.model``.
        * ``"unspecified"`` — explicit mode not declared by caller.
          Reads ``llm.model`` (Phase 1 single-model behaviour).

        The mode string is normalised silently so an unknown value
        behaves like ``"unspecified"`` (defensive — third-party
        plugins may pass arbitrary strings).
        """
        if mode not in self._VALID_MODES:
            mode = "unspecified"
        now = time.monotonic()
        cached = self._cache_by_mode.get(mode)
        if cached is not None:
            cached_p, cached_m, cached_expires = cached
            if cached_expires > now and (cached_p or cached_m):
                return cached_p, cached_m
        p, m = self.default_provider, self.default_model
        if self.settings_lookup is not None:
            try:
                prov_key, model_keys = self._ROLE_KEYS[self.role]
                p_setting = self.settings_lookup(prov_key)
                # Walk mode_key → fall back to legacy ``llm.model`` →
                # fall back to constructor default. Each step keeps
                # the value only if it's non-empty.
                m_setting: str | None = None
                for candidate_key in (model_keys[mode], "llm.model"):
                    candidate = self.settings_lookup(candidate_key)
                    if candidate:
                        m_setting = candidate
                        break
                if p_setting and m_setting:
                    p, m = p_setting, m_setting
            except Exception as exc:
                LOGGER.debug("LLMFactory settings_lookup failed: %s", exc)
        self._cache_by_mode[mode] = (p, m, now + self.lookup_ttl_seconds)
        return p, m

    def make(
        self,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        *,
        mode: str = "unspecified",
        **kwargs: Any,
    ) -> OpenAICompatibleProvider:
        """Return a configured :class:`LLMProvider`.

        Resolution priority: explicit ``provider`` / ``model`` kwargs →
        ``settings_lookup`` (cached, mode-aware) → constructor defaults
        (env vars). Raises ``ValueError`` if no provider/model can be
        resolved.

        ``mode`` is one of ``"quick"`` / ``"deep"`` / ``"unspecified"``
        and selects which settings key holds the model. See
        :meth:`_resolve_cached` for the precedence rules. Callers
        that don't care (most existing test fixtures, CLI bootstrap)
        can leave it at the default.
        """
        if provider is None or model is None:
            cached_p, cached_m = self._resolve_cached(mode=mode)
            provider = provider or cached_p
            model = model or cached_m
        if not provider or not model:
            raise ValueError(
                "LLM provider/model not configured; set TRADINGAGENTS_LLM_PROVIDER + "
                "TRADINGAGENTS_LLM_MODEL env vars, or pass explicitly to LLMFactory.make()"
            )
        # W3-D6 R8: forward the harness-level identity unless caller
        # supplied its own identity kwarg.
        kwargs.setdefault("identity", self.identity or default_app_identity())
        return OpenAICompatibleProvider(provider, model, base_url=base_url, cache=self.cache, **kwargs)

    def is_configured(self, *, mode: str = "unspecified") -> bool:
        """True iff a usable (provider, model) is currently resolvable.

        Reflects the live settings_lookup (with cache) so the
        orchestrator's :func:`maybe_degrade_to_tier1` correctly
        disables Tier 2/3 only when there is genuinely nothing
        configured. ``mode`` lets the caller check "is there at
        least a deep model configured?" without coupling to
        ``_resolve_cached`` internals.
        """
        p, m = self._resolve_cached(mode=mode)
        return bool(p and m)
