"""AppIdentity — identify this app to upstream LLM providers (W3-D6 R8).

Spec::

    R8  AppIdentity User-Agent 强制         0.5d  低

Why this exists
---------------
Provider-side logs benefit from a stable User-Agent so we can:
  - attribute traffic spikes to a specific deployment
  - debug which app version produced a bad request
  - play nice with rate-limit dashboards

Without this, the underlying SDKs send their own UA (often
``OpenAI/Python 1.x.y`` or ``python-requests/2.x.y``) which doesn't tell
the provider anything about *our* app.

The identity is intentionally minimal — just ``name + version``. We
do NOT put PII, hostname, or session IDs in the UA (provider logs
shouldn't see that, and it bloats every request).

Wire-in
-------
``OpenAICompatibleProvider.__init__`` auto-injects
``default_headers={"User-Agent": identity.user_agent}`` into the
``create_llm_client`` kwargs so the underlying langchain ``ChatOpenAI``
sends it on every request. The 4 native clients (anthropic / google /
azure / bedrock) can be wired separately in a follow-up if needed.
"""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from importlib import metadata as md
from typing import Optional

DEFAULT_NAME = "TradingAgentsPlus"
# Read version from package metadata when installed; fall back to "dev".
try:
    DEFAULT_VERSION = md.version("tradingagents")
except md.PackageNotFoundError:
    DEFAULT_VERSION = "dev"


@dataclass(frozen=True)
class AppIdentity:
    """Identifies the calling application to upstream LLM providers.

    Fields:
      name       — application name, e.g. "TradingAgentsPlus"
      version    — semver or "dev" when running from source
      user_agent — pre-built UA string ``"TradingAgentsPlus/0.1.0"``
    """

    name: str = DEFAULT_NAME
    version: str = DEFAULT_VERSION
    # Optional: caller can override the entire UA string (e.g. for
    # staging environments that want ``TradingAgentsPlus-staging/0.1.0``).
    user_agent: Optional[str] = None
    # Optional python version, included in some UAs for diagnostics.
    python_version: str = field(default_factory=lambda: platform.python_version())

    def __post_init__(self) -> None:
        # ``user_agent`` defaults to ``f"{name}/{version}"`` when not set.
        # ``frozen=True`` forbids direct assignment, so we go through
        # ``object.__setattr__``.
        if not self.user_agent:
            object.__setattr__(
                self, "user_agent", f"{self.name}/{self.version}",
            )

    def headers(self) -> dict[str, str]:
        """Return the HTTP headers this identity injects into LLM calls."""
        return {
            "User-Agent": self.user_agent or f"{self.name}/{self.version}",
            "X-App-Name": self.name,
            "X-App-Version": self.version,
        }

    def __str__(self) -> str:
        return self.user_agent or f"{self.name}/{self.version}"


def default_app_identity() -> AppIdentity:
    """Return the process-wide default identity.

    Honours ``TRADINGAGENTS_APP_NAME`` / ``TRADINGAGENTS_APP_VERSION`` env
    overrides so deployments can stamp their own identity (e.g. a
    staging build stamping itself as ``TradingAgentsPlus-staging``).
    """
    name = os.environ.get("TRADINGAGENTS_APP_NAME", DEFAULT_NAME)
    version = os.environ.get("TRADINGAGENTS_APP_VERSION", DEFAULT_VERSION)
    return AppIdentity(name=name, version=version)
