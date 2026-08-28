"""Safe, browser-facing configuration derived from the CLI catalogs."""

from __future__ import annotations

import importlib.util
import os
from typing import Any

from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS, get_model_options

OUTPUT_LANGUAGES: tuple[str, ...] = (
    "English",
    "Chinese",
    "Japanese",
    "Korean",
    "Hindi",
    "Spanish",
    "Portuguese",
    "French",
    "German",
    "Arabic",
    "Russian",
)

QUOTE_STRATEGIES = {
    "default-yfinance": {"providers": ["yfinance"]},
    "fallback-yfinance-alpha-vantage": {"providers": ["yfinance", "alpha_vantage"]},
}


def market_data_catalog(
    config: dict[str, Any], settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return non-sensitive market provider strategy and diagnostics."""
    settings = settings or {}

    def resolved(key: str, env_key: str, default: Any):
        if env_key in os.environ and os.environ[env_key]:
            return os.environ[env_key], "env"
        item = settings.get(key)
        if isinstance(item, dict) and item.get("value") is not None:
            return item["value"], item.get("source", "sqlite")
        return config.get(key, default), "default"

    strategy, source = resolved(
        "quote_strategy_id", "TRADINGAGENTS_QUOTE_STRATEGY", "default-yfinance"
    )
    ttl, ttl_source = resolved("quote_ttl_seconds", "TRADINGAGENTS_QUOTE_TTL_SECONDS", 60)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = 60
    yfinance_installed = importlib.util.find_spec("yfinance") is not None
    alpha_configured = bool(os.getenv("ALPHA_VANTAGE_API_KEY"))
    provider_ready = {
        "yfinance": yfinance_installed,
        "alpha_vantage": yfinance_installed and alpha_configured,
    }
    return {
        "quote_strategy_id": {
            "value": strategy if strategy in QUOTE_STRATEGIES else "default-yfinance",
            "source": source,
        },
        "quote_provider_chain": {
            "value": QUOTE_STRATEGIES.get(strategy, QUOTE_STRATEGIES["default-yfinance"])[
                "providers"
            ],
            "source": source,
        },
        "quote_ttl_seconds": {"value": ttl, "source": ttl_source},
        "strategies": [
            {
                "id": key,
                "providers": value["providers"],
                "available": all(provider_ready.get(p, False) for p in value["providers"]),
            }
            for key, value in QUOTE_STRATEGIES.items()
        ],
        "providers": [
            {
                "id": "yfinance",
                "installed": yfinance_installed,
                "available": yfinance_installed,
                "configured": yfinance_installed,
                "capabilities": ["quote", "candles", "identity"],
                "status": "ready" if yfinance_installed else "not_configured",
                "reason": None if yfinance_installed else "未安装 yfinance",
            },
            {
                "id": "alpha_vantage",
                "installed": True,
                "available": alpha_configured,
                "configured": alpha_configured,
                "capabilities": ["quote", "identity"],
                "status": "ready" if alpha_configured else "not_configured",
                "reason": None if alpha_configured else "未配置 Alpha Vantage API Key",
            },
        ],
    }


# Custom-only providers require another input (endpoint/model ID), so they
# remain available through the CLI but are intentionally not advertised by
# the browser until a dedicated secure settings surface exists.
WEB_PROVIDERS: tuple[str, ...] = tuple(
    provider
    for provider, modes in MODEL_OPTIONS.items()
    if any(option[1] != "custom" for options in modes.values() for option in options)
)

PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "qwen": "Qwen Global",
    "qwen-cn": "Qwen China",
    "glm": "GLM / Z.AI",
    "glm-cn": "GLM / BigModel",
    "minimax": "MiniMax Global",
    "minimax-cn": "MiniMax China",
    "ollama": "Ollama",
}


def _options(provider: str, mode: str) -> list[dict[str, str]]:
    return [
        {"label": label, "value": value}
        for label, value in get_model_options(provider, mode)
        if value != "custom"
    ]


def model_catalog(config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return a redacted provider/model catalog and valid configured defaults."""

    configured_provider = str(config.get("llm_provider") or "").lower()
    provider = configured_provider if configured_provider in WEB_PROVIDERS else "openai"
    providers: list[dict[str, Any]] = []
    for provider_key in WEB_PROVIDERS:
        quick = _options(provider_key, "quick")
        deep = _options(provider_key, "deep")
        if not quick or not deep:
            continue
        providers.append(
            {
                "value": provider_key,
                "label": PROVIDER_LABELS.get(provider_key, provider_key.title()),
                "quick_models": quick,
                "deep_models": deep,
            }
        )
    selected = next(item for item in providers if item["value"] == provider)
    quick_values = {item["value"] for item in selected["quick_models"]}
    deep_values = {item["value"] for item in selected["deep_models"]}
    quick = str(config.get("quick_think_llm") or "")
    deep = str(config.get("deep_think_llm") or "")
    defaults = {
        "provider": provider,
        "quick_model": quick if quick in quick_values else selected["quick_models"][0]["value"],
        "deep_model": deep if deep in deep_values else selected["deep_models"][0]["value"],
        "output_language": str(os.getenv("TRADINGAGENTS_OUTPUT_LANGUAGE") or config.get("output_language") or "Chinese"),
    }
    if defaults["output_language"] not in OUTPUT_LANGUAGES:
        defaults["output_language"] = "Chinese"
    return providers, defaults


def resolve_model_config(
    config: dict[str, Any], provider: str | None, quick_model: str | None, deep_model: str | None
) -> dict[str, str]:
    providers, defaults = model_catalog(config)
    provider_key = (provider or defaults["provider"]).lower()
    selected = next((item for item in providers if item["value"] == provider_key), None)
    if selected is None:
        raise ValueError("invalid analysis configuration")
    quick_values = {item["value"] for item in selected["quick_models"]}
    deep_values = {item["value"] for item in selected["deep_models"]}
    quick = quick_model or (
        defaults["quick_model"]
        if provider_key == defaults["provider"]
        else selected["quick_models"][0]["value"]
    )
    deep = deep_model or (
        defaults["deep_model"]
        if provider_key == defaults["provider"]
        else selected["deep_models"][0]["value"]
    )
    if quick not in quick_values or deep not in deep_values:
        raise ValueError("invalid analysis configuration")
    return {"provider": provider_key, "quick_model": quick, "deep_model": deep}
