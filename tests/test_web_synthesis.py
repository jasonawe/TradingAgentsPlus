from pathlib import Path

from web.synthesis import generate_executive_summary, save_executive_summary


class FakeResponse:
    content = "## 综合研判\n\n建议保持仓位，关注成交量和下行风险。"


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return FakeResponse()


def test_summary_uses_deep_model_and_requested_language():
    llm = FakeLLM()
    summary = generate_executive_summary(
        llm,
        ticker="AAPL",
        output_language="Chinese",
        final_state={
            "market_report": "Price is above the 50-day average.",
            "investment_plan": "Prefer a measured entry.",
            "trader_investment_plan": "Scale in after confirmation.",
            "final_trade_decision": "Hold",
        },
    )
    assert summary.startswith("## 综合研判")
    assert "Chinese" in llm.prompts[0]
    assert "AAPL" in llm.prompts[0]


def test_summary_is_saved_as_a_separate_report_file(tmp_path):
    path = save_executive_summary(tmp_path, "# Executive Summary\n\nHold")
    assert path == Path(tmp_path) / "executive_summary.md"
    assert path.read_text(encoding="utf-8") == "# Executive Summary\n\nHold"
