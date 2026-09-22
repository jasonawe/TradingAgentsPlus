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
    # §Step 24 — handle Pydantic BaseModel. Tools like get_quote
    # return a ``QuoteResult(BaseModel)`` that lives in
    # ``state.tool_results[i]["result"]``. The previous walk only
    # recursed into dict/list/str/numbers, which silently dropped the
    # entire QuoteResult and left the haystack empty — making
    # claim_audit return 0.0 with every number flagged as
    # unsupported. Use ``model_dump()`` (v2) / ``.dict()`` (v1) so
    # numeric fields (price / change_pct / amplitude / …) reach the
    # number-extraction regex.
    if value is None:
        return
    # Pydantic v2 first (``model_dump``), then v1 (``dict``).
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            _walk(value.model_dump(), out)
            return
        except Exception:
            pass
    if hasattr(value, "dict") and callable(value.dict) and not isinstance(value, type):
        try:
            _walk(value.dict(), out)
            return
        except Exception:
            pass
    # dataclass fallback for non-Pydantic structured objects.
    if hasattr(value, "__dict__") and not isinstance(value, type):
        try:
            _walk(vars(value), out)
            return
        except Exception:
            pass
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
    - exact substring match (after stripping leading ``+`` so a sign
      prefix in the answer (``+0.22%``) still matches ``0.22`` in data)
    - rounded-to-2dp match (so "5" in answer matches "5.00" in data)
    - percentage match ("5.2%" in answer matches "5.2" in data)
    - numeric tokenisation of multi-value haystack strings (e.g.
      ``"price=26.9 open=26.83"``) so each numeric value inside is
      compared individually — previously the whole blob was fed to
      ``_to_float`` which returned None and skipped the float
      comparison path silently.
    """
    n_float = _to_float(num)
    # Strip optional sign prefix when doing substring search so
    # ``+0.22`` still matches ``0.22``. The float comparison path is
    # sign-agnostic.
    sign_stripped = num.lstrip("+")
    needle = sign_stripped.rstrip("%")
    # Tokenise haystack strings into their numeric substrings once per
    # call so multi-value blobs (``price=26.9 open=26.83 ...``) yield
    # per-number floats we can compare against ``n_float``.
    haystack_numbers: list[float] = []
    for h in haystack:
        if n_float is None:
            # Numeric token matching not applicable; fall back to
            # substring search below.
            pass
        else:
            for m in _NUMBER_RE.finditer(h or ""):
                tok = m.group(1).replace(",", "")
                h_float = _to_float(tok)
                if h_float is not None:
                    haystack_numbers.append(h_float)
        if needle in (h or ""):
            return True
    if n_float is None:
        return False
    # Numeric comparison against the tokenised haystack.
    for h_float in haystack_numbers:
        if abs(h_float - n_float) < 1e-6:
            return True
        # Percent normalization: if the answer says "5.2%" and the
        # data has "5.2" we treat them as the same number.
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
    haystack = _flatten_tool_values(tool_results)
    # Lazy import to avoid circular dependency at module load
    from tradingagents.agent_harness.verification.chinese_numbers import (
        extract_chinese_numbers,
        chinese_in_haystack,
    )
    arabic_nums = extract_numbers(answer)
    chinese_nums = extract_chinese_numbers(answer)
    total_count = len(arabic_nums) + len(chinese_nums)
    if total_count == 0:
        # No numerical claims → nothing to verify, perfect score.
        return 1.0, []
    if not haystack:
        # Tools returned nothing useful; we can't verify any number.
        unsupported = list(arabic_nums) + [str(n) for n in chinese_nums]
        return 0.0, unsupported
    unsupported: list[str] = []
    # Arabic numbers
    for n in arabic_nums:
        if not _number_in_haystack(n, haystack):
            unsupported.append(n)
    # Chinese numbers
    for n in chinese_nums:
        if not chinese_in_haystack(n, haystack):
            unsupported.append(str(n))
    matched = total_count - len(unsupported)
    score = matched / total_count
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
