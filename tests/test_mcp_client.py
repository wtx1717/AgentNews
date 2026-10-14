from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentnews.config import Settings
from agentnews.tools import (
    MCPToolArgumentError,
    MCPToolDefinition,
    MCPToolDiscoveryError,
    MCPToolSchemaError,
    MCPToolsClient,
)
from agentnews.tools import mcp_client


def _settings(**overrides):
    values = {
        "mcp_search_server_url": "https://mcp.example.test/mcp",
        "mcp_search_api_key": "secret-key",
        "mcp_search_timeout": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _tool(name: str, *, description: str = "tool", schema: dict | None = None):
    return SimpleNamespace(
        name=name,
        description=description,
        input_schema=schema or {"type": "object", "properties": {}, "additionalProperties": False},
    )


def _page(tools, cursor=None):
    return SimpleNamespace(tools=tools, next_cursor=cursor)


def _harness(monkeypatch, pages, *, response=None):
    state = {"list_calls": [], "calls": [], "closed": False, "http_closed": False}

    class FakeHTTP:
        def __init__(self, *, headers, timeout):
            state["headers"] = headers
            state["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            state["http_closed"] = True

    class FakeClient:
        def __init__(self, transport, *, read_timeout_seconds):
            state["transport"] = transport
            state["read_timeout_seconds"] = read_timeout_seconds

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            state["closed"] = True

        async def list_tools(self, *, cursor=None):
            state["list_calls"].append(cursor)
            index = 0 if cursor is None else int(cursor)
            return pages[index]

        async def call_tool(self, name, arguments):
            state["calls"].append((name, arguments))
            return response or SimpleNamespace(is_error=False, structured_content={"ok": True})

    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", FakeHTTP)
    monkeypatch.setattr(mcp_client, "streamable_http_client", lambda url, *, http_client: (url, http_client))
    monkeypatch.setattr(mcp_client, "ProtocolClient", FakeClient)
    return state


def test_discovers_all_paginated_tools_and_preserves_metadata(monkeypatch):
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    state = _harness(
        monkeypatch,
        [
            _page([_tool("tavily_search", description="search")], "1"),
            _page(
                [
                    _tool("tavily_extract", description="extract", schema=schema),
                    _tool("tavily_crawl", description="crawl"),
                    _tool("tavily_map", description="map"),
                    _tool("tavily_research", description="research"),
                ]
            ),
        ],
    )

    async def run():
        async with MCPToolsClient(_settings()) as client:
            return client.tools

    definitions = asyncio.run(run())
    assert [tool.name for tool in definitions] == [
        "tavily_search",
        "tavily_extract",
        "tavily_crawl",
        "tavily_map",
        "tavily_research",
    ]
    assert definitions[1].description == "extract"
    assert definitions[1].input_schema == schema
    assert state["list_calls"] == [None, "1"]
    assert state["closed"] is True
    assert state["http_closed"] is True


def test_call_tool_validates_arguments_and_dispatches_actual_name(monkeypatch):
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
    state = _harness(monkeypatch, [_page([_tool("tavily_extract", schema=schema)])])

    async def run():
        async with MCPToolsClient(_settings()) as client:
            result = await client.call_tool("tavily_extract", {"url": "https://example.test"})
            with pytest.raises(MCPToolArgumentError):
                await client.call_tool("tavily_extract", {})
            return result

    result = asyncio.run(run())
    assert '"ok":true' in result
    assert state["calls"] == [("tavily_extract", {"url": "https://example.test"})]


@pytest.mark.parametrize(
    "pages, expected",
    [
        ([_page([_tool("same")], "1"), _page([_tool("same")])], "重复"),
        ([_page([_tool("tavily_search")], "1"), _page([], "1")], "游标"),
    ],
)
def test_invalid_discovery_is_rejected_without_leaking_credentials(monkeypatch, pages, expected):
    state = _harness(monkeypatch, pages)

    async def run():
        async with MCPToolsClient(_settings()):
            pass

    with pytest.raises(MCPToolDiscoveryError, match=expected) as exc_info:
        asyncio.run(run())
    assert "secret-key" not in str(exc_info.value)
    assert state["closed"] is True
    assert state["http_closed"] is True


def test_invalid_schema_is_rejected(monkeypatch):
    state = _harness(monkeypatch, [_page([_tool("bad", schema={"type": "string"})])])

    async def run():
        async with MCPToolsClient(_settings()):
            pass

    with pytest.raises(MCPToolSchemaError):
        asyncio.run(run())
    assert state["closed"] is True
    assert state["http_closed"] is True


def test_definition_is_immutable_and_exposes_wire_schema():
    definition = MCPToolDefinition("tool", "description", {"type": "object"})
    assert definition.inputSchema == definition.input_schema
