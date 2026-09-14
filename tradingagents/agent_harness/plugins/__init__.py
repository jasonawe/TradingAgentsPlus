"""Plugin system — P6 (v3 spec §5.4)."""
from .base import Plugin
from .registry import PluginRegistry

__all__ = ["Plugin", "PluginRegistry"]
