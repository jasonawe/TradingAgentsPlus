"""Step 30 — claim audit detector.

The synthesizer LLM sometimes fabricates numbers ("招商银行 2025 PE
预计 12.5x") that don't appear in any tool result. The existing
citation_score only checks whether the LLM listed which tools were used
— it does NOT check whether specific numerical claims in the
prose match the tool data.

``claim_audit_score`` walks the answer text, extracts every numeric
token, and verifies each one appears somewhere in the tool result
payloads (as a literal or near-literal). Returns:

- score: 0.0..1.0 (ratio of grounded numbers)
- unsupported: list of numbers that don't match any tool result

When ``unsupported`` is non-empty AND the answer claims authority
("预期", "预计", "估计", "target", "estimate"), we treat the
answer as ungrounded regardless of the score.

Why this is separate from citation_score
---------------------------------------
- citation_score = "did the LLM list which tools it used?"
- claim_audit = "did the numbers in the prose actually come from those tools?"

Both are needed because a citation block with no numbers can pass
citation_score while the prose still contains hallucinated values.
"""
from __future__ import annotations

import re
from typing import Iterable

# Numbers to extract. We accept:
#   - integers (123, -7)
#   - decimals (12.5, 0.04, .5)
#   - percentages (5.2%)
#   - currency-stripped (¥40.71 → 40.71, $185 → 185)
# We deliberately do NOT extract:
#   - dates (2026-09-18) — too noisy
#   - version strings (v1.2.3) — semantic ambiguity
#   - very large numbers with > 6 digits (likely ids / hashes)
_NUMBER_RE = re.compile(
    r"(?<![\d.])"           # not preceded by another digit/dot
    r"([+\-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?)"  # 1-3 digits, optional ,group, optional .frac, optional %
    r"(?![\d.])"            # not followed by another digit/dot
)

# Phrases that signal the LLM is making a forward-looking claim
# (which we cannot verify against tool data). If the answer contains
# these AND unsupported numbers, we escalate to ungrounded.
_FORWARD_CLAIM_PHRASES = (
    "预计", "预期", "估计", "预计将", "有望", "大概率",
    "target price", "estimate", "guidance", "forecast",
)


def extract_numbers(text: str) -> list[str]:
    """Return the list of numeric tokens in ``text`` in order."""
    if not text:
        return []
    out: list[str] = []
    for m in _NUMBER_RE.finditer(text):
        n = m.group(1).replace(",", "")
        # Skip pure year-like 4-digit numbers (2026 etc) unless the
        # context is clearly not a year. We use a simple heuristic:
        # 4-digit numbers between 1900 and 2099 are likely years.
        try:
            iv = int(n.rstrip("%"))
        except ValueError:
            out.append(n)
            continue
        if 1900 <= iv <= 2099 and len(n.rstrip("%")) == 4:
            continue
        out.append(n)
    return out


def _to_float(s: str) -> float | None:
    s = s.rstrip("%").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _flatten_tool_values(tool_results: Iterable[dict]) -> list[str]:
    """Recursively pull every string/number value out of tool_results.

    Used as the haystack for claim matching. Returns values in their
    string form so we can do simple substring matching.
    """
    out: list[str] = []
    for r in tool_results:
        if not isinstance(r, dict):
            continue
        # Top-level keys we care about
        for k in ("result", "data", "raw", "summary", "name", "text"):
            v = r.get(k)
            if v is None:
                continue
            _walk(v, out)
    return out


def _walk(value: object, out: list[str]) -> None:
    if isinstance(value, dict):
        for vv in value.values():
            _walk(vv, out)
    elif isinstance(value, (list, tuple)):
        for vv in value:
            _walk(vv, out)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        out.append(str(value))
    elif isinstance(value, str):
        out.append(value)


def _number_in_haystack(num: str, haystack: list[str]) -> bool:
    """Return True if ``num`` matches something in ``haystack``.

    Accepts:
    - exact substring match
    - rounded-to-2dp match (so "5" in answer matches "5.00" in data)
    - percentage match ("5.2%" in answer matches "5.2" in data)
    """
    n_float = _to_float(num)
    if n_float is None:
        # Non-numeric token (shouldn't happen after extract_numbers)
        return any(num in h for h in haystack)
    needle = num.rstrip("%")
    for h in haystack:
        if needle in h:
            return True
        # Try matching as a number against numeric tokens
        h_float = _to_float(h)
        if h_float is None:
            continue
        if abs(h_float - n_float) < 1e-6:
            return True
        # Percent normalization: if the answer says "5.2%" and the data
        # has "5.2" we treat them as the same number.
        if num.endswith("%") and abs(h_float - n_float) < 1e-6:
            return True
        # Loose match: 2-decimal tolerance for floats
        if abs(h_float - n_float) < 0.01 and n_float < 1000:
            return True
    return False


def claim_audit_score(
    answer: str,
    tool_results: Iterable[dict],
) -> tuple[float, list[str]]:
    """Score (0..1) the fraction of numbers in ``answer`` that appear
    in some tool result.

    Returns ``(score, unsupported_numbers)``. ``unsupported_numbers``
    is the list of numeric tokens that did NOT match any tool value.
    """
    numbers = extract_numbers(answer)
    if not numbers:
        # No numerical claims → nothing to verify, perfect score.
        return 1.0, []
    haystack = _flatten_tool_values(tool_results)
    if not haystack:
        # Tools returned nothing useful; we can't verify any number.
        return 0.0, list(numbers)
    unsupported: list[str] = []
    for n in numbers:
        if not _number_in_haystack(n, haystack):
            unsupported.append(n)
    score = (len(numbers) - len(unsupported)) / len(numbers)
    return score, unsupported


def has_forward_claim(answer: str) -> bool:
    """Return True if ``answer`` contains a forward-looking claim phrase.

    Used to escalate unsupported numbers into a hard ungrounded
    verdict (LLM is making a forecast it cannot back up).
    """
    if not answer:
        return False
    lo = answer.lower()
    return any(p in lo for p in _FORWARD_CLAIM_PHRASES)
