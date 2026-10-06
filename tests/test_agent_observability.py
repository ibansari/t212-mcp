"""Agent tooling and tracing: no LLM, browser or database needed."""

import logging

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from t212_mcp.lookthrough import known_sources
from t212_mcp.lookthrough.agent_tools import is_data_request, rank_data_requests
from t212_mcp.lookthrough.tracing import AgentTracer, summarize

INVESCO_API = "https://dng-api.invesco.com/cache/v1/accounts/en_GB/shareclasses/IE000UOXRAM8/holdings/index?idType=isin"


def test_fetch_requests_count_as_data_whatever_their_content_type():
    assert is_data_request(INVESCO_API, "text/plain;charset=UTF-8", "fetch")  # Invesco sends JSON as text/plain
    assert is_data_request("https://x.com/files/holdings.xlsx", "", "document")
    assert not is_data_request("https://x.com/logo.png", "image/png", "image")
    assert not is_data_request("https://www.google-analytics.com/collect", "text/plain", "xhr")


def test_holdings_requests_are_listed_first_largest_first_without_duplicates():
    seen = [("https://api/x/nav", "200 nav", 900), ("https://api/x/portfolio-stats", "200 stats", 2),
            (INVESCO_API, "200 holdings", 49048), ("https://api/x/nav", "200 nav", 900)]
    assert rank_data_requests(seen) == ["200 holdings", "200 stats", "200 nav"]


def test_known_sources_match_issuer_names():
    assert [r.issuer for r in known_sources.matching("Invesco MSCI ACWI Islamic M-Series (Acc)")] == ["Invesco"]
    assert [r.issuer for r in known_sources.matching("HSBC MSCI USA Islamic Screened (Acc)")] == ["HSBC"]
    assert known_sources.matching("Vanguard FTSE All-World") == []
    assert "dng-api.invesco.com" in known_sources.hint("Invesco Dow Jones Islamic Global Developed Markets")
    assert known_sources.hint("Vanguard FTSE All-World") == ""


@tool
def inspect_source(url: str) -> str:
    """Fake tool."""
    return "<untrusted_web_content>\nformat: json\nholdings: list[1257]\n</untrusted_web_content>"


def fake_agent(model):
    async def run(inputs, config):
        await model.ainvoke("find holdings", config=config)
        await inspect_source.ainvoke({"url": INVESCO_API}, config=config)
        return "done"
    return RunnableLambda(run, name="agent")


async def test_tracer_logs_each_step_and_keeps_the_run_tree(caplog):
    model = FakeMessagesListChatModel(responses=[AIMessage(
        content="", tool_calls=[{"name": "inspect_source", "args": {"url": INVESCO_API}, "id": "c1"}],
        usage_metadata={"input_tokens": 900, "output_tokens": 300, "total_tokens": 1200})])
    tracer = AgentTracer("IGDA", "discover", start_tokens=500)
    with caplog.at_level(logging.INFO, logger="t212_mcp.agent"):
        await fake_agent(model).ainvoke({}, config={"callbacks": [tracer]})
    lines = [r.getMessage() for r in caplog.records]
    assert "IGDA discover model 1,200 tok (1,700 total) → inspect_source" in lines
    assert any(line.startswith("IGDA discover step 1 inspect_source") and "dng-api" in line for line in lines)
    assert "IGDA discover   → format: json" in lines
    assert lines[-1].startswith("IGDA discover done: 1 model calls")

    (tree,) = tracer.trees
    assert tree["name"] == "agent" and [c["type"] for c in tree["children"]] == ["llm", "tool"]
    assert summarize(tree) == {"model_calls": 1, "tools": {"inspect_source": 1}, "tokens": 1200}


async def test_failed_runs_are_kept_with_their_error():
    async def boom(inputs, config):
        raise RuntimeError("token budget exhausted (433949 > 400000)")

    tracer = AgentTracer("MWIM", "discover")
    with pytest.raises(RuntimeError):
        await RunnableLambda(boom, name="agent").ainvoke({}, config={"callbacks": [tracer]})
    assert "token budget exhausted" in tracer.trees[0]["error"]
