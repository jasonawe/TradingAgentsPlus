from types import SimpleNamespace

import pytest

from tradingagents.dataflows import alpha_vantage_common
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.llm_clients.anthropic_client import NormalizedChatAnthropic
from tradingagents.llm_clients.azure_client import NormalizedAzureChatOpenAI
from tradingagents.llm_clients.google_client import NormalizedChatGoogleGenerativeAI
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI


class Response:
    content = "ok"


def configure_deadline_policy(
    llm, *, deadline_supplier, timeout_cap, checkpoint=None
):
    object.__setattr__(llm, "_deadline_supplier", deadline_supplier)
    object.__setattr__(llm, "_request_timeout_cap", timeout_cap)
    object.__setattr__(llm, "_external_request_checkpoint", checkpoint)


@pytest.mark.parametrize(
    ("normalized", "parent_path"),
    [
        (NormalizedChatOpenAI, "tradingagents.llm_clients.openai_client.ChatOpenAI.invoke"),
        (
            NormalizedChatAnthropic,
            "tradingagents.llm_clients.anthropic_client.ChatAnthropic.invoke",
        ),
        (
            NormalizedChatGoogleGenerativeAI,
            "tradingagents.llm_clients.google_client.ChatGoogleGenerativeAI.invoke",
        ),
        (
            NormalizedAzureChatOpenAI,
            "tradingagents.llm_clients.azure_client.AzureChatOpenAI.invoke",
        ),
    ],
)
def test_llm_timeout_is_resolved_for_each_actual_invocation(
    monkeypatch, normalized, parent_path
):
    captured = []
    checkpoints = []
    remaining = iter((20.0, 4.0))

    def fake_invoke(self, input, config=None, **kwargs):
        captured.append(kwargs.get("timeout"))
        return Response()

    monkeypatch.setattr(parent_path, fake_invoke)
    llm = object.__new__(normalized)
    configure_deadline_policy(
        llm,
        deadline_supplier=lambda: next(remaining),
        timeout_cap=10.0,
        checkpoint=lambda: checkpoints.append("checkpoint"),
    )
    assert llm.invoke("first").content == "ok"
    assert llm.invoke("second").content == "ok"
    assert captured == [10.0, 4.0]
    assert checkpoints == ["checkpoint"] * 4


def test_llm_without_deadline_supplier_preserves_existing_timeout_behavior(monkeypatch):
    captured = []

    def fake_invoke(self, input, config=None, **kwargs):
        captured.append(kwargs)
        return Response()

    monkeypatch.setattr(
        "tradingagents.llm_clients.openai_client.ChatOpenAI.invoke", fake_invoke
    )
    llm = object.__new__(NormalizedChatOpenAI)
    configure_deadline_policy(llm, deadline_supplier=None, timeout_cap=30.0)
    llm.invoke("prompt")
    assert captured == [{}]


def test_bedrock_resolves_timeout_at_each_invoke(monkeypatch):
    from tradingagents.llm_clients import bedrock_client

    captured = []
    remaining = iter((12.0, 2.0))

    class FakeBedrock:
        def invoke(self, input, config=None, **kwargs):
            captured.append(kwargs.get("timeout"))
            return Response()

    monkeypatch.setattr(bedrock_client, "_BEDROCK_CLASS", None)
    monkeypatch.setitem(
        __import__("sys").modules,
        "langchain_aws",
        SimpleNamespace(ChatBedrockConverse=FakeBedrock),
    )
    normalized = bedrock_client._bedrock_class()
    llm = normalized()
    configure_deadline_policy(
        llm,
        deadline_supplier=lambda: next(remaining),
        timeout_cap=5.0,
    )
    llm.invoke("first")
    llm.invoke("second")
    assert captured == [5.0, 2.0]


def test_alpha_vantage_caps_each_request_by_current_remaining_deadline(monkeypatch):
    remaining = iter((12.0, 3.0))
    captured = []
    checkpoints = []

    monkeypatch.setattr(
        alpha_vantage_common,
        "get_request_timeout",
        lambda normal: min(normal, next(remaining)),
        raising=False,
    )
    monkeypatch.setattr(
        alpha_vantage_common,
        "external_request_checkpoint",
        lambda: checkpoints.append("checkpoint"),
        raising=False,
    )

    class FakeResponse:
        text = "timestamp,close\n2026-08-31,1\n"

        def raise_for_status(self):
            return None

    def fake_get(*args, **kwargs):
        captured.append(kwargs["timeout"])
        return FakeResponse()

    monkeypatch.setattr(alpha_vantage_common.requests, "get", fake_get)
    alpha_vantage_common._make_api_request("TIME_SERIES_DAILY", {}, api_key="x")
    alpha_vantage_common._make_api_request("TIME_SERIES_DAILY", {}, api_key="x")
    assert captured == [12.0, 3.0]
    assert checkpoints == ["checkpoint"] * 4


def test_non_web_dataflow_config_clears_prior_run_scoped_deadline_callbacks():
    set_config(
        {
            "deadline_supplier": lambda: 1.0,
            "external_request_checkpoint": lambda: None,
        }
    )
    set_config({"news_article_limit": 20})
    config = get_config()
    assert "deadline_supplier" not in config
    assert "external_request_checkpoint" not in config
