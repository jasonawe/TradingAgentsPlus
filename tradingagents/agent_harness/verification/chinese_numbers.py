"""Step 36 — Chinese number parser for claim_audit.

The Step 30 claim_audit extracts Arabic numbers ("40.71", "+1.04%")
from the LLM answer and verifies each against tool data. It does
NOT handle Chinese number phrases like:

- 三百亿 (300 × 10^8 = 30,000,000,000)
- 一万手 (10,000 手)
- 百分之五 (5%)
- 五点二 (5.2)
- 三成 (30%)
- 负七 (-7)
- 两百 (200)

When the LLM says "招商银行市值三百亿" and the tool returned
"market_cap": 30_000_000_000, claim_audit should treat these as
the same number — otherwise the user's answer gets flagged as
fabricated even though the data matches.

This module converts Chinese number phrases to floats so
claim_audit can match them against tool values. Coverage:

- 一/二/.../九  → 1..9
- 十/百/千/万/亿/兆 → 10 / 100 / 1e3 / 1e4 / 1e8 / 1e12
- 零 = 0
- 两 = 2 (colloquial)
- 百分之N = N / 100 (percentage)
- N成 = N × 0.1 (ratio: 五成 = 50%)
- "X点Y" decimal support (三点五 = 3.5)
- 负X / X负 = negative
- Range matches like "三百亿到四百亿" → returns both endpoints

Out of scope
------------
- Chinese ordinals (第一, 第三)
- Date phrases (二〇二六年 — handled by year filter)
- Currency unit conversion (元/美元/港元) — claim_audit compares
  raw numbers; unit conversion is out of scope for hallucination
  detection (would require knowing which unit the user means).
"""
from __future__ import annotations

import re
from typing import Iterable

_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0,
    "一": 1, "壹": 1,
    "二": 2, "贰": 2, "两": 2, "兩": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

_UNITS: dict[str, int] = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1_000, "仟": 1_000,
    "万": 10_000, "萬": 10_000,
    "亿": 100_000_000, "億": 100_000_000,
    "兆": 1_000_000_000_000, "万亿": 100_000_000_000,
}

# Decimal point — "三点五" = 3.5
_DECIMAL_POINT = "点"

# "百分之N" → N / 100, "五成" → 5 * 0.1 = 0.5
_PERCENT_RE = re.compile(r"百分之([零一二三四五六七八九十百千万亿两]+)")
_RATIO_RE = re.compile(r"([零一二三四五六七八九十百千万亿两]+)成")

# Negative markers
_NEGATIVE_RE = re.compile(r"负([零一二三四五六七八九十百千万亿两]+)|([零一二三四五六七八九十百千万亿两]+)负")

# Chinese number candidates — match the longest plausible phrase
# greedily. We pre-filter via the blacklist below; this regex just
# pulls candidate spans.
#
# A phrase is either:
#   - 百分之N  (percentage)
#   - N成      (ratio, with no preceding 百分之)
#   - 负N      (negative marker, handled by _parse_negative)
#   - N点M     (decimal)
#   - N        (digits/units: 三百亿, 一万, 七, 三千零五)
#
# The regex is greedy on each alternative so "三百零五" matches as
# one phrase, not three separate ones.
_NUMBER_CANDIDATE_RE = re.compile(
    r"(百分之[零一二三四五六七八九十百千万亿两]{1,15})"
    r"|(负[零一二三四五六七八九十百千万亿]{1,15})"
    r"|([零一二三四五六七八九十百千万亿点两成]{2,20})"
    r"|(零)"
)

# Skip words that LOOK like Chinese numbers but aren't (e.g. ordinals,
# years, generic phrases). We keep this conservative.
_BLACKLIST_SUBSTRINGS = (
    "第二", "第三", "第四", "第五",
    "第一", "二〇", "三〇", "四〇", "五〇",
    "一一", "二二", "三三",
)


def _parse_simple(s: str) -> float | None:
    """Parse a simple Chinese number phrase to float.

    Handles both the multiplicative form (三百亿 = 3×100×1e8) and
    the "点"-decimal form (三点五 = 3.5). Returns None if the
    string isn't a valid Chinese number phrase.
    """
    if not s:
        return None
    # "X成" ratio — 五成 = 0.5
    if s.endswith("成"):
        inner = s[:-1]
        v = _parse_simple(inner)
        if v is None:
            return None
        return v * 0.1
    # Decimal: split on 点
    if _DECIMAL_POINT in s:
        left, right = s.split(_DECIMAL_POINT, 1)
        l = _parse_simple(left) if left else 0.0
        r_str = right
        if not r_str or any(c not in _DIGITS for c in r_str):
            return None
        r = float(_DIGITS[r_str[0]])
        for c in r_str[1:]:
            r = r * 10 + _DIGITS[c]
        return (l or 0.0) + r / (10 ** len(r_str))
    # "X成" — handled separately
    # Walk left-to-right building (value, next unit)
    total = 0
    section = 0  # accumulator within the current 万/亿 segment
    current = 0  # last digit
    for c in s:
        if c in _DIGITS:
            current = _DIGITS[c]
        elif c in _UNITS:
            unit = _UNITS[c]
            if unit >= 10_000:
                # 万 / 亿 flushes the section
                section = (section + current) * (unit if section + current else 1)
                if section == 0:
                    section = unit  # bare "万" = 10000
                total += section
                section = 0
                current = 0
            elif current == 0 and c == "十":
                # "十三" = 13 (十 = 10 standalone)
                current = 1
                section += current * unit
                current = 0
            else:
                section += current * unit
                current = 0
        else:
            return None
    section += current
    if total == 0:
        if section:
            return float(section)
        # Bare 零 / empty after parsing → 0
        if s == "零":
            return 0.0
        return None
    return float(total + section)


def _parse_percent(s: str) -> float | None:
    m = _PERCENT_RE.search(s)
    if not m:
        return None
    v = _parse_simple(m.group(1))
    if v is None:
        return None
    return v / 100.0


def _parse_ratio(s: str) -> float | None:
    m = _RATIO_RE.search(s)
    if not m:
        return None
    v = _parse_simple(m.group(1))
    if v is None:
        return None
    return v * 0.1


def _parse_negative(s: str) -> tuple[float | None, str]:
    """Detect a leading 负 marker; return (signed_value, rest)."""
    if s.startswith("负"):
        rest = s[1:]
        v = _parse_simple(rest)
        if v is None:
            return None, s
        return -v, ""
    m = re.match(r"^(.+?)负$", s)
    if m:
        v = _parse_simple(m.group(1))
        if v is None:
            return None, s
        return -v, ""
    return None, s


def extract_chinese_numbers(text: str) -> list[float]:
    """Pull every Chinese-number phrase from ``text`` and return floats.

    Phrases that don't parse cleanly are silently dropped. Duplicates
    are kept in source order so the caller can compare position-by-
    position with Arabic extractions.

    Out-of-scope phrases (ordinals, years, common words) are filtered
    via a small blacklist of substrings.
    """
    if not text:
        return []
    out: list[float] = []
    # 1. Match candidates greedily. The regex has 3 alternatives so
    # findall returns the matched group for whichever matched (other
    # groups are None). We pick the non-empty one.
    for match in _NUMBER_CANDIDATE_RE.finditer(text):
        cand = next((g for g in match.groups() if g), "")
        if not cand:
            continue
        if any(bad in cand for bad in _BLACKLIST_SUBSTRINGS):
            continue
        # Skip phrases ending in 成 that's the ratio marker (e.g.
        # "五成") — we still want the digit portion to be at least 1
        # char before the marker.
        # 2. Try percentage and ratio first.
        pct = _parse_percent(cand)
        if pct is not None:
            out.append(pct)
            continue
        rat = _parse_ratio(cand)
        if rat is not None:
            out.append(rat)
            continue
        # 3. Negative
        if cand.startswith("负"):
            v = _parse_simple(cand[1:])
            if v is not None:
                out.append(-v)
            continue
        # 4. Plain parse
        v = _parse_simple(cand)
        if v is not None:
            out.append(v)
    return out


def chinese_in_haystack(value: float, haystack: list[str]) -> bool:
    """Return True if ``value`` matches something in ``haystack``.

    Tries (in order):
    1. Zero check (``零`` substring)
    2. Integer-form Chinese (e.g. ``三百亿`` for ``3e10``)
    3. Fractional form (Arabic ``0.05``)
    4. Percentage equivalence — if value < 1, also try ``value * 100``
       as integer (so ``百分之五`` = 0.05 matches ``change_pct`` = 5.0)
    5. Loose 1e-6 tolerance for all comparisons (catches small float drift)
    """
    if value == 0:
        return any("零" in h for h in haystack)
    abs_val = abs(value)
    # Integer-form Chinese
    if abs_val >= 1 and abs_val == int(abs_val):
        candidate = _int_to_chinese(int(abs_val))
        if candidate and any(candidate in h for h in haystack):
            return True
    # Fractional form (Arabic "0.05")
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    if any(s in h for h in haystack):
        return True
    # Percentage equivalence: 百分之五 = 0.05 should match change_pct = 5.0
    if abs_val < 1:
        scaled = abs_val * 100
        if scaled == int(scaled):
            scaled_str = str(int(scaled))
            if any(scaled_str in h for h in haystack):
                return True
    # Loose float tolerance against numeric tokens
    for h in haystack:
        try:
            h_float = float(h)
        except (TypeError, ValueError):
            continue
        if abs(h_float - value) < 1e-6:
            return True
    return False


def _int_to_chinese(n: int) -> str:
    """Render a non-negative integer as a Chinese number phrase.

    Limited to values up to 10^16 (one hundred trillion). Out-of-range
    returns "" so the caller falls back to Arabic match. Arbitrary-
    width leading digits work via recursion (``三百亿`` for ``3e8``).
    """
    if n < 0 or n > 10**16:
        return ""
    if n == 0:
        return "零"
    # Walk large units first, then small ones. Each step pulls the
    # head digit count and recurses / maps it via _DIGITS_INV.
    out_parts: list[str] = []
    remaining = n
    # 兆 / 亿 / 万
    for unit_val, unit_name in ((10**12, "兆"), (10**8, "亿"), (10**4, "万")):
        if remaining >= unit_val:
            head, remaining = divmod(remaining, unit_val)
            out_parts.append(_int_to_chinese(head) + unit_name)
            if 0 < remaining < unit_val // 10:
                out_parts.append("零")
    # 千 / 百 / 十
    for unit_val, unit_name in ((10**3, "千"), (10**2, "百"), (10**1, "十")):
        if remaining >= unit_val:
            head, remaining = divmod(remaining, unit_val)
            if head == 1 and unit_name == "十":
                out_parts.append("十")
            else:
                out_parts.append(_DIGITS_INV[head] + unit_name)
            if 0 < remaining < unit_val // 10:
                out_parts.append("零")
    if remaining > 0:
        out_parts.append(_DIGITS_INV[remaining])
    return "".join(out_parts)


_DIGITS_INV: dict[int, str] = {
    0: "零", 1: "一", 2: "二", 3: "三", 4: "四",
    5: "五", 6: "六", 7: "七", 8: "八", 9: "九",
}
