"""Step 22 P1 — citation scoring for synthesizer answers.

The synthesizer system prompt asks the LLM to include a `> 来源: ...`
block listing every tool that contributed data. This module scores
how well an answer meets that contract so the L3 judge can down-weight
uncited answers.

Score formula (0..1):

    covered = #expected_sources whose tool name appears in the
              citation block of the answer
    coverage = covered / max(len(expected_sources), 1)
    has_block = 1.0 if a citation block exists, 0.6 if not
    return coverage * has_block

Empty / trivial answers (``answer == ""`` or no data-bearing tools
in expected_sources) return 1.0 — citation is irrelevant when
the answer is a CRUD ack.
"""
from __future__ import annotations

import re
from typing import Iterable


# Tools that produce *data*, not just CRUD acks. When none of these
# show up in expected_sources the answer is by definition a CRUD ack
# and citation scoring is bypassed.
DATA_TOOLS = frozenset({
    "get_quote", "get_history", "get_fundamentals", "get_news",
    "get_report", "list_alpha_factors", "evaluate_alpha",
    "list_reports", "list_notes", "list_alerts", "list_watchlist",
    "list_scheduled_tasks", "list_runs",
})

# Accept either the markdown blockquote form ("\> 来源: ...") or a
# heading form ("## 来源\n\n\> ..." / "### 来源\n..."). The heading
# can sit on its own line; we capture the line(s) after it.
_CITATION_BLOCK = re.compile(
    r"^\s*>\s*(?:来源|source[s]?)\s*[:：]\s*(.+?)$"
    r"|^\s*##\s*(?:来源|Data\s+Sources?)\s*$\s*\n((?:^\s*>.+?$\n?)+)",
    re.MULTILINE | re.IGNORECASE,
)


def _has_data_sources(expected: Iterable[str]) -> bool:
    return any(t.lower() in DATA_TOOLS for t in expected)


def citation_score(answer: str, expected_sources: Iterable[str]) -> float:
    """Score how well the answer cites its data sources.

    Returns a float in [0, 1]. 1.0 = every expected tool named.
    0.0 = no citation block when one was expected.

    When the query has no data-bearing tools in the expected list
    (e.g. a pure CRUD ack), returns 1.0 — citation irrelevant.
    """
    expected = [t for t in expected_sources if t]
    if not expected or not _has_data_sources(expected):
        return 1.0

    answer = answer or ""
    block_match = _CITATION_BLOCK.search(answer)
    if not block_match:
        # No citation block at all → hard fail (still get a fraction for
        # the prose mentioning tool names, but the user can't verify
        # which call produced which number).
        hits = sum(
            1 for t in expected
            if re.search(re.escape(t), answer, re.IGNORECASE)
        )
        return 0.5 * (hits / len(expected))

    cited_line = (block_match.group(1) or block_match.group(2) or "").strip()
    covered = sum(
        1 for t in expected
        if re.search(re.escape(t), cited_line, re.IGNORECASE)
    )
    return covered / len(expected)
