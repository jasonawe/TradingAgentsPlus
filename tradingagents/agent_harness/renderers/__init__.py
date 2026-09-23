"""Friendly renderers for tool results."""
from .history_sparkline import render_history_card, infer_history_params  # noqa: F401
from .compare_sparkline import render_compare_card  # noqa: F401
from .friendly_cards import (  # noqa: F401
    render_quote_card,
    render_fundamentals_card,
    render_news_card,
    render_alpha_card,
    render_ack_card,
)
