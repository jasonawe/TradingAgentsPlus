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

    # §P3-4 — ``role`` strings accepted by :meth:`_resolve`. The main
    # LLM uses ``"main"``; the L3 judge uses ``"judge"``. Roles map to
    # the SettingsRepository keys ``llm.{role}_provider`` /
    # ``llm.{role}_model`` (with ``main`` dropping the ``main_`` infix
    # for readability — keys are ``llm.provider`` / ``llm.model``).
    _ROLE_KEYS = {
        "main": ("llm.provider", "llm.model"),
        "judge": ("llm.judge_provider", "llm.judge_model"),
    }

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
        # (provider, model, expires_at) — refreshed lazily by
        # _resolve_cached(). ``expires_at = 0.0`` is a sentinel for
        # "no cached value yet" so the first call always re-resolves.
        self._cache_provider: str = ""
        self._cache_model: str = ""
        self._cache_expires_at: float = 0.0

    def invalidate(self) -> None:
        """Drop the cached (provider, model) so the next :meth:`make`
        re-reads settings. Useful for tests and for the PATCH endpoint
        to apply changes immediately rather than after TTL.
        """
        self._cache_expires_at = 0.0

    def _resolve_cached(self) -> tuple[str, str]:
        """Resolve the effective (provider, model) for this factory's
        role. Cached for :attr:`lookup_ttl_seconds`.

        Priority on each cache miss: settings_lookup → constructor
        default (which already absorbed env vars at __init__).
        """
        now = time.monotonic()
        if self._cache_expires_at > now and (self._cache_provider or self._cache_model):
            return self._cache_provider, self._cache_model
        p, m = self.default_provider, self.default_model
        if self.settings_lookup is not None:
            try:
                prov_key, model_key = self._ROLE_KEYS[self.role]
                p_setting = self.settings_lookup(prov_key)
                m_setting = self.settings_lookup(model_key)
                if p_setting and m_setting:
                    p, m = p_setting, m_setting
            except Exception as exc:
                LOGGER.debug("LLMFactory settings_lookup failed: %s", exc)
        self._cache_provider = p
        self._cache_model = m
        self._cache_expires_at = now + self.lookup_ttl_seconds
        return p, m

    def make(
        self,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> OpenAICompatibleProvider:
        """Return a configured :class:`LLMProvider`.

        Resolution priority: explicit ``provider`` / ``model`` kwargs →
        ``settings_lookup`` (cached) → constructor defaults (env vars).
        Raises ``ValueError`` if no provider/model can be resolved.
        """
        if provider is None or model is None:
            cached_p, cached_m = self._resolve_cached()
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

    def is_configured(self) -> bool:
        """True iff a usable (provider, model) is currently resolvable.

        Reflects the live settings_lookup (with cache) so the
        orchestrator's :func:`maybe_degrade_to_tier1` correctly
        disables Tier 2/3 only when there is genuinely nothing
        configured.
        """
        p, m = self._resolve_cached()
        return bool(p and m)
