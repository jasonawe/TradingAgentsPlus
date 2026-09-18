"""Step 23 — tool capability enum.

A capability is a coarse-grained tag the harness uses to:
- filter tool lists per intent / surface
- pick the right ``display_view`` template
- apply the right permission gate (CRUD vs read)

Adding new capabilities: just extend this enum. No code elsewhere
needs to change — capabilities flow through ``metadata["capabilities"]``.
"""
from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    """Coarse-grained capability tags for tools.

    The enum inherits from ``str`` so the values can flow through
    JSON / SQLite / markdown without an explicit ``.value`` call.
    """

    # Data fetch
    QUOTE = "quote"
    HISTORY = "history"
    FUNDAMENTALS = "fundamentals"
    NEWS = "news"
    ALPHA = "alpha"

    # CRUD entities (write side)
    NOTE = "note"             # notes CRUD
    ALERT = "alert"           # price alerts
    WATCHLIST = "watchlist"   # watchlist CRUD
    SCHEDULED = "scheduled"   # scheduled tasks
    RUN = "run"               # analysis runs
    REPORT = "report"         # report CRUD

    # Cross-cutting
    META = "meta"             # tool itself (list tools, health, etc)
    REPORT_READ = "report_read"  # special-case for get_report
