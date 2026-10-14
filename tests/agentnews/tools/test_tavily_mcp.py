from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentnews.config import Settings
from agentnews.tools import (
    OnlineSearchResult,
    SearchConfigurationError,
    SearchToolDiscoveryError,
    SearchToolSchemaError,
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


def _tool(name="tavily-search", schema=None, *, title=None, description=""):
    if schema is None:
        schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 5, "maximum": 20},
                "topic": {"type": "string", "enum": ["general", "news"]},
                "search_depth": {"type": "string", "enum": ["basic", "advanced"]},
            },
            "required": ["query"],
        }
    return SimpleNamespace(
        name=name,
        title=title,
        description=description,
        input_schema=schema,
    )


def _page(tools, next_cursor=None):
    return SimpleNamespace(tools=tools, next_cursor=next_cursor)


def _harness(
    monkeypatch,
    *,
    response=None,
    failure=None,
    list_failure=None,
    enter_failure=None,
    tool_pages=None,
):
    state = {"http_closed": False, "client_closed": False, "calls": [], "list_calls": []}
    pages = tool_pages or [_page([_tool()])]

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

        async def list_tools(self, *, cursor=None):
            state["list_calls"].append(cursor)
            if list_failure:
                raise list_failure
            index = 0 if cursor is None else int(cursor)
            return pages[index]

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


@pytest.mark.parametrize("tool_name", ["tavily_search", "search_tavily", "tavilySearch"])
def test_search_maps_general_arguments_and_normalizes_structured_results(monkeypatch, tool_name):
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
        tool_pages=[_page([_tool(tool_name)])],
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
            tool_name,
            {
                "query": "latest AI news",
                "max_results": 8,
                "topic": "general",
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


def test_search_maps_schema_aliases_and_omits_unsupported_parameters(monkeypatch):
    schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "limit": {"type": "integer", "minimum": 5, "maximum": 20},
            "topic": {"type": "string", "enum": ["general"]},
            "search_depth": {"type": "string", "enum": ["advanced"]},
            "time_range": {"type": "string"},
        },
        "required": ["q", "topic"],
    }
    state = _harness(
        monkeypatch,
        response=_response(structured_content={"results": []}),
        tool_pages=[_page([_tool("search_tavily", schema)])],
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("AI news", limit=9)

    asyncio.run(run())
    assert state["calls"] == [
        ("search_tavily", {"q": "AI news", "limit": 9, "topic": "general"})
    ]


def test_search_discovers_tool_from_later_page(monkeypatch):
    state = _harness(
        monkeypatch,
        response=_response(structured_content={"results": []}),
        tool_pages=[
            _page([_tool("tavily_extract", description="Extract page content")], "1"),
            _page([_tool("search_tavily")]),
        ],
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("news")

    asyncio.run(run())
    assert state["list_calls"] == [None, "1"]
    assert state["calls"][0][0] == "search_tavily"


def test_search_discovery_accepts_explicit_description_metadata(monkeypatch):
    state = _harness(
        monkeypatch,
        response=_response(structured_content={"results": []}),
        tool_pages=[_page([_tool("tavily", description="Tavily Search")])],
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("news")

    asyncio.run(run())
    assert state["calls"][0][0] == "tavily"


def test_missing_search_tool_fails_without_calling_tool(monkeypatch):
    state = _harness(
        monkeypatch,
        tool_pages=[
            _page(
                [
                    _tool("tavily_extract", description="Extract page content"),
                    _tool("web_search", description="Search the web"),
                ]
            )
        ],
    )

    async def run():
        async with TavilyMCPClient(_settings()):
            pass

    with pytest.raises(SearchToolDiscoveryError, match="候选数：0"):
        asyncio.run(run())
    assert state["calls"] == []
    assert state["client_closed"] is True
    assert state["http_closed"] is True


def test_multiple_search_tools_across_pages_fail_without_calling_tool(monkeypatch):
    state = _harness(
        monkeypatch,
        tool_pages=[
            _page([_tool("tavily_search")], "1"),
            _page([_tool("search_tavily")]),
        ],
    )

    async def run():
        async with TavilyMCPClient(_settings()):
            pass

    with pytest.raises(SearchToolDiscoveryError, match="候选数：2"):
        asyncio.run(run())
    assert state["list_calls"] == [None, "1"]
    assert state["calls"] == []
    assert state["client_closed"] is True
    assert state["http_closed"] is True


def test_general_topic_const_is_supported(monkeypatch):
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
            "topic": {"type": "string", "const": "general"},
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced", "fast", "ultra-fast"],
            },
            "include_domains": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["query"],
    }
    state = _harness(
        monkeypatch,
        response=_response(structured_content={"results": []}),
        tool_pages=[_page([_tool("tavily_search", schema)])],
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("AI news", limit=10)

    asyncio.run(run())
    assert state["calls"] == [
        (
            "tavily_search",
            {
                "query": "AI news",
                "max_results": 10,
                "topic": "general",
                "search_depth": "basic",
            },
        )
    ]


def test_topic_is_omitted_when_schema_does_not_declare_it(monkeypatch):
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }
    state = _harness(
        monkeypatch,
        response=_response(structured_content={"results": []}),
        tool_pages=[_page([_tool("tavily_search", schema)])],
    )

    async def run():
        async with TavilyMCPClient(_settings()) as search:
            await search.search("AI news", limit=7)

    asyncio.run(run())
    assert state["calls"] == [
        ("tavily_search", {"query": "AI news", "max_results": 7})
    ]


@pytest.mark.parametrize(
    "topic_schema",
    [
        {"type": "string", "const": "news"},
        {"type": "string", "enum": ["news"]},
    ],
)
def test_general_topic_must_be_allowed_by_schema(monkeypatch, topic_schema):
    properties = {
        "query": {"type": "string"},
        "topic": topic_schema,
    }
    state = _harness(
        monkeypatch,
        tool_pages=[
            _page(
                [
                    _tool(
                        schema={
                            "type": "object",
                            "properties": properties,
                            "required": ["query"],
                        }
                    )
                ]
            )
        ],
    )

    async def run():
        async with TavilyMCPClient(_settings()):
            pass

    with pytest.raises(SearchToolSchemaError, match="topic=general") as exc_info:
        asyncio.run(run())
    assert "test-api-key" not in str(exc_info.value)
    assert state["calls"] == []
    assert state["client_closed"] is True
    assert state["http_closed"] is True


def test_query_and_result_count_schema_constraints_are_enforced(monkeypatch):
    state = _harness(
        monkeypatch,
        tool_pages=[
            _page(
                [
                    _tool(
                        schema={
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "minLength": 4},
                                "max_results": {"type": "integer", "maximum": 6},
                                "topic": {"type": "string", "enum": ["general"]},
                            },
                        }
                    )
                ]
            )
        ],
    )

    async def run(query, limit):
        async with TavilyMCPClient(_settings()) as search:
            await search.search(query, limit)

    for query, limit in (("AI", 5), ("news", 7)):
        with pytest.raises(SearchToolSchemaError):
            asyncio.run(run(query, limit))
    assert state["calls"] == []


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


def test_tool_discovery_timeout_is_mapped_and_closes_connection(monkeypatch):
    state = _harness(monkeypatch, list_failure=TimeoutError("secret list timeout"))

    async def run():
        async with TavilyMCPClient(_settings()):
            pass

    with pytest.raises(SearchTimeoutError) as exc_info:
        asyncio.run(run())
    assert "secret list timeout" not in str(exc_info.value)
    assert "test-api-key" not in str(exc_info.value)
    assert state["calls"] == []
    assert state["client_closed"] is True
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
