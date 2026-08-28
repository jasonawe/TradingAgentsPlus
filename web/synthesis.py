"""Generate a concise, second-pass executive report from a completed run."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return str(value)


def _source_material(final_state: dict[str, Any], limit: int = 16000) -> str:
    sections = [
        ("Market analysis", final_state.get("market_report")),
        ("Sentiment analysis", final_state.get("sentiment_report")),
        ("News analysis", final_state.get("news_report")),
        ("Fundamentals analysis", final_state.get("fundamentals_report")),
        ("Research manager plan", final_state.get("investment_plan")),
        ("Trading plan", final_state.get("trader_investment_plan")),
        ("Risk debate", (final_state.get("risk_debate_state") or {}).get("history")),
        ("Final decision", final_state.get("final_trade_decision")),
    ]
    material = "\n\n".join(f"### {name}\n{_text(value)}" for name, value in sections if _text(value).strip())
    return material[:limit]


def generate_executive_summary(
    llm: Any,
    *,
    ticker: str,
    output_language: str,
    final_state: dict[str, Any],
) -> str:
    """Ask the selected deep-thinking model for a compact decision brief."""

    prompt = f"""You are the senior investment editor for {ticker}. Produce a concise executive summary from the completed multi-agent research below.

Write the entire answer in {output_language}. Do not invent facts, prices, or certainty that are absent from the source material. Resolve disagreements explicitly and preserve the original final decision when evidence does not justify changing it.

Use five Markdown sections covering: executive summary, recommendation, key evidence, main risks, and action plus confidence. Translate the section headings into the requested output language.

Keep the answer under 500 words. Include the decision rating (Buy, Overweight, Hold, Underweight, or Sell), a practical next action, and a confidence level (Low, Medium, or High).

Source material:
{_source_material(final_state)}"""
    response = llm.invoke(prompt)
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "\n".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
    return _text(content).strip()


def save_executive_summary(report_dir: str | Path, summary: str) -> Path:
    path = Path(report_dir) / "executive_summary.md"
    path.write_text(summary.strip(), encoding="utf-8")
    return path
