from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agentnews.agent import AgentRunner
from agentnews.config import Settings
from agentnews.tools.path_security import ManagedPathResolver
from agentnews.tools import OnlineSearchResult


def _settings(**overrides):
    values = {"agent_max_tool_calls": 8}
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []
        self.tools = None

    def bind_tools(self, tools):
        self.tools = list(tools)
        return self

    async def ainvoke(self, messages):
        self.messages.append(list(messages))
        return self.responses.pop(0)


def _tool_call(name, args, call_id):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def test_direct_answer_does_not_initialize_search(monkeypatch):
    model = FakeModel([AIMessage(content="直接回答")])

    def unexpected_search(_settings):
        pytest.fail("search client must be lazy")

    runner = AgentRunner(model, settings=_settings(), search_client_factory=unexpected_search)
    result = asyncio.run(runner.ainvoke("hello"))

    assert result.text == "直接回答"
    assert result.tool_calls == 0
    assert result.stop_reason == "completed"
    assert isinstance(model.messages[0][0], HumanMessage)
    assert {tool.name for tool in model.tools} == {
        "web_search", "list_dir", "read_file", "search_content", "write_file"
    }


def test_file_tool_result_is_returned_to_model(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("hello", encoding="utf-8")
    model = FakeModel(
        [
                AIMessage(content="", tool_calls=[_tool_call("read_file", {"path": "note.txt"}, "call-1")]),
            AIMessage(content="文件内容是 hello"),
        ]
    )

    result = asyncio.run(
        AgentRunner(
            model,
            settings=_settings(),
            resolver=ManagedPathResolver(data_root),
        ).ainvoke("读取 note.txt")
    )

    assert result.text == "文件内容是 hello"
    assert result.tool_calls == 1
    tool_message = model.messages[1][-1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content)["result"]["content"] == "hello"


def test_multiple_tools_run_in_order_and_results_are_replayed(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("needle", encoding="utf-8")
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("list_dir", {"path": "."}, "call-1"),
                    _tool_call("search_content", {"keyword": "needle", "dir": "."}, "call-2"),
                ]
            ),
            AIMessage(content="已检查目录并完成搜索"),
        ]
    )

    result = asyncio.run(
        AgentRunner(model, settings=_settings(), resolver=ManagedPathResolver(data_root)).ainvoke("检查")
    )

    assert result.tool_calls == 2
    replayed = model.messages[1]
    assert [message.tool_call_id for message in replayed if isinstance(message, ToolMessage)] == [
        "call-1", "call-2"
    ]
    assert [message.name for message in replayed if isinstance(message, ToolMessage)] == [
        "list_dir", "search_content"
    ]


def test_model_can_request_tools_across_multiple_turns(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "note.txt").write_text("needle", encoding="utf-8")
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("list_dir", {"path": "."}, "call-1")],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        "search_content",
                        {"keyword": "needle", "dir": "."},
                        "call-2",
                    )
                ],
            ),
            AIMessage(content="连续工具调用完成"),
        ]
    )

    runner = AgentRunner(
        model,
        settings=_settings(),
        resolver=ManagedPathResolver(data_root),
    )
    result = asyncio.run(runner.ainvoke("列出并搜索文件"))

    assert result.tool_calls == 2
    assert result.text == "连续工具调用完成"
    assert len(model.messages) == 3
    assert [
        message.tool_call_id
        for message in model.messages[2]
        if isinstance(message, ToolMessage)
    ] == ["call-1", "call-2"]


def test_search_tool_uses_lazy_client_and_serializes_results():
    class FakeSearch:
        def __init__(self, settings):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def search(self, query, limit=5):
            assert query == "AI"
            assert limit == 6
            return [
                OnlineSearchResult(
                    title="Headline",
                    url="https://example.com",
                    snippet="Summary",
                    published_at=None,
                    source_site="example.com",
                    searched_at=datetime.now(UTC),
                )
            ]

    clients = []

    def factory(settings):
        client = FakeSearch(settings)
        clients.append(client)
        return client

    model = FakeModel(
        [
                AIMessage(content="", tool_calls=[_tool_call("web_search", {"query": "AI", "limit": 6}, "search-1")]),
            AIMessage(content="搜索完成"),
        ]
    )
    result = asyncio.run(
        AgentRunner(model, settings=_settings(), search_client_factory=factory).ainvoke("搜索 AI")
    )

    assert result.text == "搜索完成"
    assert len(clients) == 1 and clients[0].closed is True
    payload = json.loads(model.messages[1][-1].content)
    assert payload["results"][0]["title"] == "Headline"


def test_search_error_is_sanitized_and_client_is_closed():
    class FakeSearch:
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def search(self, query, limit=5):
            raise RuntimeError("upstream failure test-api-key")

    clients = []

    def factory(settings):
        client = FakeSearch()
        clients.append(client)
        return client

    model = FakeModel(
        [
            AIMessage(content="", tool_calls=[_tool_call("web_search", {"query": "AI"}, "search-1")]),
            AIMessage(content="搜索暂不可用"),
        ]
    )
    asyncio.run(
        AgentRunner(model, settings=_settings(), search_client_factory=factory).ainvoke("搜索")
    )

    tool_result = model.messages[1][-1].content
    assert "upstream failure" not in tool_result
    assert "test-api-key" not in tool_result
    assert json.loads(tool_result)["error"]["code"] == "search_error"
    assert clients[0].closed is True


def test_unknown_tool_and_invalid_arguments_are_safe():
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("not_registered", {}, "call-1"),
                    _tool_call("read_file", {"unexpected": True}, "call-2"),
                ]
            ),
            AIMessage(content="已处理")
        ]
    )
    result = asyncio.run(AgentRunner(model, settings=_settings()).ainvoke("处理"))

    assert result.tool_calls == 2
    errors = [json.loads(message.content)["error"] for message in model.messages[1] if isinstance(message, ToolMessage)]
    assert [error["code"] for error in errors] == ["unknown_tool", "invalid_arguments"]


def test_tool_call_limit_stops_without_an_extra_model_call(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("list_dir", {"path": "."}, "call-1"),
                    _tool_call("list_dir", {"path": "."}, "call-2"),
                ]
            ),
            AIMessage(content="不应调用")
        ]
    )
    result = asyncio.run(
        AgentRunner(
            model,
            settings=_settings(agent_max_tool_calls=1),
            resolver=ManagedPathResolver(data_root),
        ).ainvoke("持续调用")
    )

    assert result.tool_calls == 1
    assert result.stop_reason == "max_tool_calls"
    assert "工具调用达到上限" in result.text
    assert '"ok":true' in result.text
    assert len(model.messages) == 1


def test_sync_invoke_rejects_running_event_loop():
    runner = AgentRunner(FakeModel([AIMessage(content="ok")]), settings=_settings())

    async def run():
        with pytest.raises(RuntimeError, match="active event loop"):
            runner.invoke("hello")

    asyncio.run(run())


def test_sync_invoke_runs_direct_answer():
    runner = AgentRunner(FakeModel([AIMessage(content="同步回答")]), settings=_settings())

    assert runner.invoke("hello").text == "同步回答"
