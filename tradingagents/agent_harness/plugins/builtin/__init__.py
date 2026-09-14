"""Built-in plugins: Quant / News / Alert (v3 spec §5.4).

Each plugin provides tool(s) + prompt overrides; N63 fix means agents are
NOT re-implemented in plugins — plugins just *reference* core agents.
"""
from .quant import QuantPlugin
from .news import NewsPlugin
from .alert import AlertPlugin

__all__ = ["QuantPlugin", "NewsPlugin", "AlertPlugin"]
