from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage, ToolMessage

from agentnews.agent import AgentRunner, ToolRegistrationError
from agentnews.config import Settings
from agentnews.tools import MCPToolDefinition


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tools = None
        self.messages = []

    def bind_tools(self, tools):
        self.tools = list(tools)
        return self

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        return self.responses.pop(0)


class FakeMCP:
    def __init__(self, settings, definitions, calls):
        self.definitions = definitions
        self.calls = calls
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    @property
    def tools(self):
        return tuple(self.definitions)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"ok": True, "result": {"tool": name, "arguments": arguments}})


def _settings():
    return Settings(
        _env_file=None,
        mcp_search_server_url="https://mcp.example.test/mcp",
        mcp_search_api_key="test-key",
    )


def _call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_all_mcp_tools_are_bound_and_continue_to_final_answer():
    definitions = [
        MCPToolDefinition(
            name,
            name + " description",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        )
        for name in ("tavily_search", "tavily_extract", "tavily_crawl", "tavily_map", "tavily_research")
    ]
    calls = []
    client = FakeMCP(_settings(), definitions, calls)
    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_call("tavily_extract", {"url": "https://example.test"}, "mcp-1")]),
            AIMessage(content="完成"),
        ]
    )

    result = asyncio.run(
        AgentRunner(
            model,
            settings=_settings(),
            mcp_client_factory=lambda settings: client,
        ).ainvoke("提取页面")
    )

    assert {tool.name for tool in model.tools} == {
        "list_dir", "read_file", "search_content", "write_file",
        "tavily_search", "tavily_extract", "tavily_crawl", "tavily_map", "tavily_research",
    }
    extract = next(tool for tool in model.tools if tool.name == "tavily_extract")
    assert extract.description == "tavily_extract description"
    assert extract.args_schema["required"] == ["url"]
    assert calls == [("tavily_extract", {"url": "https://example.test"})]
    assert isinstance(model.messages[1][-1], ToolMessage)
    assert result.text == "完成"
    assert client.closed is True


def test_mcp_local_tool_name_collision_is_rejected():
    client = FakeMCP(
        _settings(),
        [MCPToolDefinition("read_file", "collision", {"type": "object", "properties": {}})],
        [],
    )
    model = FakeModel([AIMessage(content="unused")])

    try:
        asyncio.run(
            AgentRunner(
                model,
                settings=_settings(),
                mcp_client_factory=lambda settings: client,
            ).ainvoke("测试")
        )
    except ToolRegistrationError as exc:
        assert "read_file" in str(exc)
    else:
        raise AssertionError("expected tool collision")
