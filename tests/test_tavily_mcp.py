from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentnews.config import Settings
from agentnews.tools import (
    OnlineSearchResult,
    SearchConfigurationError,
    SearchTimeoutError,
    SearchUnavailableError,
    TavilyMCPClient,
)
from agentnews.tools import tavily_mcp


def _settings(**overrides):
    values = {
        "mcp_search_server_url": "https://mcp.tavily.com/mcp",
        "mcp_search_api_key": "test-api-key",
        "mcp_search_timeout": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _harness(monkeypatch, *, response=None, failure=None, enter_failure=None):
    state = {"http_closed": False, "client_closed": False, "calls": []}

    class FakeHTTPClient:
        def __init__(self, *, headers, timeout):
            state["headers"] = headers
            state["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            state["http_closed"] = True

    class FakeMCPClient:
        def __init__(self, transport, *, read_timeout_seconds):
            state["transport"] = transport
            state["read_timeout_seconds"] = read_timeout_seconds

        async def __aenter__(self):
            if enter_failure:
                raise enter_failure
            return self

        async def __aexit__(self, *_):
            state["client_closed"] = True

        async def call_tool(self, name, arguments):
            state["calls"].append((name, arguments))
            if failure:
                raise failure
            return response

    monkeypatch.setattr(tavily_mcp.httpx2, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(
        tavily_mcp,
        "streamable_http_client",
        lambda url, *, http_client: (url, http_client),
    )
    monkeypatch.setattr(tavily_mcp, "Client", FakeMCPClient)
    return state


def _response(*, structured_content=None, text=None, is_error=False):
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        structured_content=structured_content,
        content=content,
        is_error=is_error,
    )


def test_search_maps_news_arguments_and_normalizes_structured_results(monkeypatch):
    state = _harness(
        monkeypatch,
        response=_response(
            structured_content={
                "results": [
                    {
                        "title": "Example headline",
                        "url": "https://news.example.com/story/1",
                        "content": "A short summary.",
                        "published_date": "2026-10-14",
                    }
                ]
            }
        ),
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            results = await search.search("  latest AI news  ", limit=8)
        return results

    results = asyncio.run(run())

    assert state["headers"] == {"Authorization": "Bearer test-api-key"}
    assert state["transport"][0] == "https://mcp.tavily.com/mcp"
    assert state["timeout"] == 2
    assert state["read_timeout_seconds"] == 2
    assert state["calls"] == [
        (
            "tavily-search",
            {
                "query": "latest AI news",
                "max_results": 8,
                "topic": "news",
                "search_depth": "basic",
            },
        )
    ]
    assert len(results) == 1
    assert isinstance(results[0], OnlineSearchResult)
    assert results[0].title == "Example headline"
    assert results[0].snippet == "A short summary."
    assert results[0].published_at == "2026-10-14"
    assert results[0].source_site == "news.example.com"
    assert results[0].searched_at.tzinfo is not None
    assert state["client_closed"] is True
    assert state["http_closed"] is True


def test_search_normalizes_tavily_text_results(monkeypatch):
    _harness(
        monkeypatch,
        response=_response(
            text=(
                "Detailed Results:\n\n"
                "1. Title: News item\n"
                "URL: https://example.com/news\n"
                "Content: First line\ncontinued summary\n"
                "Published Date: 2026-10-13"
            )
        ),
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            return await search.search("news")

    results = asyncio.run(run())
    assert len(results) == 1
    assert results[0].title == "News item"
    assert results[0].snippet == "First line\ncontinued summary"
    assert results[0].published_at == "2026-10-13"


def test_empty_search_results_return_empty_list(monkeypatch):
    _harness(monkeypatch, response=_response(structured_content={"results": []}))

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            return await search.search("news")

    assert asyncio.run(run()) == []


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("secret upstream timeout"), tavily_mcp.httpx2.ReadTimeout("slow response")],
)
def test_search_timeout_is_mapped_without_leaking_upstream_message(monkeypatch, failure):
    state = _harness(monkeypatch, failure=failure)

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("news")

    with pytest.raises(SearchTimeoutError) as exc_info:
        asyncio.run(run())
    assert "secret upstream timeout" not in str(exc_info.value)
    assert state["client_closed"] is True
    assert state["http_closed"] is True


def test_unavailable_service_error_is_mapped_without_leaking(monkeypatch):
    _harness(monkeypatch, failure=RuntimeError("authorization test-api-key failed"))

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("news")

    with pytest.raises(SearchUnavailableError) as exc_info:
        asyncio.run(run())
    assert "test-api-key" not in str(exc_info.value)
    assert "authorization" not in str(exc_info.value)


def test_mcp_error_result_is_mapped(monkeypatch):
    _harness(monkeypatch, response=_response(is_error=True))

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("news")

    with pytest.raises(SearchUnavailableError, match="返回错误"):
        asyncio.run(run())


def test_connection_timeout_closes_http_client_and_maps_error(monkeypatch):
    state = _harness(monkeypatch, enter_failure=TimeoutError("slow handshake"))

    async def run():
        async with TavilyMCPClient(_settings()):
            pass

    with pytest.raises(SearchTimeoutError) as exc_info:
        asyncio.run(run())
    assert "slow handshake" not in str(exc_info.value)
    assert state["http_closed"] is True


def test_missing_search_configuration_is_sanitized():
    async def run():
        async with TavilyMCPClient(Settings(_env_file=None)):
            pass

    with pytest.raises(
        SearchConfigurationError,
        match="MCP_SEARCH_SERVER_URL, MCP_SEARCH_API_KEY",
    ):
        asyncio.run(run())


@pytest.mark.parametrize(
    "query,limit",
    [(" ", 5), ("news", 4), ("news", 21), ("news", True)],
)
def test_search_rejects_invalid_query_or_limit(monkeypatch, query, limit):
    _harness(monkeypatch, response=_response(structured_content={"results": []}))

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search(query, limit)

    with pytest.raises(ValueError):
        asyncio.run(run())
