from copy import deepcopy

import tradingagents.default_config as default_config

# Use default config but allow it to be overridden
_config: dict | None = None


def initialize_config():
    """Initialize the configuration with default values."""
    global _config
    if _config is None:
        _config = deepcopy(default_config.DEFAULT_CONFIG)


def set_config(config: dict):
    """Update the configuration with custom values.

    Dict-valued keys (e.g. ``data_vendors``) are merged one level deep so a
    partial update like ``{"data_vendors": {"core_stock_apis": "alpha_vantage"}}``
    keeps the other nested keys from the default; scalar keys are replaced.
    """
    global _config
    initialize_config()
    incoming = deepcopy(config)
    for key in ("deadline_supplier", "external_request_checkpoint"):
        if key not in incoming:
            _config.pop(key, None)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(_config.get(key), dict):
            _config[key].update(value)
        else:
            _config[key] = value


def get_config() -> dict:
    """Get the current configuration."""
    if _config is None:
        initialize_config()
    return deepcopy(_config)


def get_request_timeout(normal_timeout: float) -> float:
    """Cap one data-provider request by the current Web run deadline."""

    config = get_config()
    supplier = config.get("deadline_supplier")
    if not callable(supplier):
        return float(normal_timeout)
    return min(float(normal_timeout), max(0.0, float(supplier())))


def external_request_checkpoint() -> None:
    checkpoint = get_config().get("external_request_checkpoint")
    if callable(checkpoint):
        checkpoint()


# Initialize with default config
initialize_config()
